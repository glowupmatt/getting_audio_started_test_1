import os
import time
import torchaudio
import json
from inference import model_fn, predict_fn, output_fn
from pathlib import Path
import boto3
import io
import uuid
from dotenv import load_dotenv
import tempfile

load_dotenv()

def get_audio_info(input_path):
    """Get audio sample rate using ffprobe"""
    import subprocess
    import json
    
    cmd = [
        'ffprobe',
        '-v', 'quiet',
        '-print_format', 'json',
        '-show_streams',
        input_path
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    data = json.loads(result.stdout)
    
    for stream in data['streams']:
        if stream['codec_type'] == 'audio':
            return int(stream['sample_rate'])
    
    return 44100  # fallback to CD quality if detection fails

def convert_m4a_to_mp3(input_path, output_path):
    """Convert M4A file to MP3 format using ffmpeg"""
    import subprocess
        
    # Get original sample rate
    original_sample_rate = get_audio_info(input_path)
    print(f"Detected original sample rate: {original_sample_rate} Hz")
    
    try:
        # Use ffmpeg to convert M4A to MP3
        subprocess.run([
            'ffmpeg',
            '-i', input_path,  # Input file
            '-acodec', 'libmp3lame',  # Use MP3 codec
            '-ab', '320k',  # Set bitrate to 320kbps
            '-ar', str(original_sample_rate),
            '-y',  # Overwrite output file if it exists
            output_path
        ], check=True)
        return True, original_sample_rate
    except subprocess.CalledProcessError as e:
        print(f"Error converting file: {str(e)}")
        return False, None

def upload_to_s3(file_content, filename, bucket_name=None):
    """Upload a file to S3 and return the public URL"""
    if bucket_name is None:
        bucket_name = os.getenv('AWS_BUCKET_NAME')
    
    s3_client = boto3.client('s3')
    safe_filename = f"{uuid.uuid4().hex}_{filename}"
    s3_path = f"stems/{safe_filename}"
    
    # Upload to S3
    s3_upload_buffer = io.BytesIO(file_content)
    s3_client.upload_fileobj(
        s3_upload_buffer,
        bucket_name,
        s3_path,
        ExtraArgs={
            'ContentType': 'audio/mpeg',
            'ACL': 'public-read'
        }
    )
    
    # Return the public URL
    return f"https://{bucket_name}.s3.{os.getenv('AWS_DEFAULT_REGION')}.amazonaws.com/{s3_path}"

def test_model_locally(audio_path, output_format='wav', mode='2'):
    """Test the model locally with a sample audio file and upload results to S3
    
    Args:
        audio_path (str): Path to input audio file
        output_format (str): Output format - 'wav' or 'mp3'
        mode (str): '2' for vocals/instrumental, '4' for all stems
        
    Returns:
        dict: Dictionary of stem names and their S3 URLs
    """
    print("Loading pre-trained HTDemucs model...")
    model = model_fn()
    
    original_sample_rate = get_audio_info(audio_path)
    print(f"Original sample rate: {original_sample_rate} Hz")
    
    # Handle M4A files using a temporary file
    if audio_path.lower().endswith('.m4a'):
        print("Converting M4A to MP3...")
        with tempfile.NamedTemporaryFile(delete=False, suffix='.mp3') as temp_file:
            success, _ = convert_m4a_to_mp3(audio_path, temp_file.name)
            if success:
                audio_path = temp_file.name
            else:
                raise Exception("Failed to convert M4A to MP3")
    
    print(f"Processing {audio_path}...")
    try:
        # Load and normalize audio
        waveform, sample_rate = torchaudio.load(audio_path)
        waveform = waveform / waveform.abs().max()
        if waveform.shape[0] == 1:
            waveform = waveform.repeat(2, 1)
            
        print("Separating stems...")
        separated_stems = predict_fn(waveform, model)
        
        print("Preparing output...")
        accept_type = 'audio/mpeg' if output_format == 'mp3' else 'audio/wav'
        
        # Define stems based on mode
        if mode == '2':
            stems_to_process = {
                'vocals': separated_stems['vocals'],
                'instrumental': sum(separated_stems[k] for k in ['drums', 'bass', 'other'])
            }
        else:
            stems_to_process = {
                'vocals': separated_stems['vocals'],
                'drums': separated_stems['drums'],
                'bass': separated_stems['bass'],
                'other': separated_stems['other']
            }
        
        # Store URLs for stems
        stem_urls = {}
        
        # Upload each stem with retry logic
        for stem_name, stem_data in stems_to_process.items():
            retry_count = 0
            max_retries = 5
            
            while retry_count < max_retries:
                try:
                    # Convert stem to bytes
                    output_bytes = output_fn({stem_name: stem_data}, accept_type, original_sample_rate)
                    
                    # Generate unique filename
                    safe_filename = f"{uuid.uuid4().hex}_{Path(audio_path).stem}"
                    stem_filename = f"{stem_name}_{safe_filename}.{output_format}"
                    
                    # Upload to S3
                    stem_urls[stem_name] = upload_to_s3(
                        output_bytes,
                        stem_filename
                    )
                    print(f"Uploaded {stem_name} stem to: {stem_urls[stem_name]}")
                    break
                    
                except Exception as e:
                    retry_count += 1
                    if retry_count == max_retries:
                        raise Exception(f"Failed to upload {stem_name} after {max_retries} attempts: {str(e)}")
                    time.sleep(2 ** retry_count)  # Exponential backoff
        
        print("Done! All stems uploaded successfully")
        return json.dumps({
            'stems': stem_urls,
            'original_sample_rate': original_sample_rate,
            'output_format': output_format
        })
    
    except Exception as e:
        print(f"Error processing file: {str(e)}")
        raise
    finally:
        # Clean up any temporary files
        if 'temp_file' in locals():
            os.unlink(temp_file.name)

if __name__ == "__main__":
    # Example usage with simplified parameters
    test_model_locally(
        audio_path=r"C:\Users\thatg\Downloads\PinkPantheress, Sam Gellaitry - Picture in my mind (Official Video).mp3",  # Use raw string for Windows path
        output_format='mp3',  # or 'wav'
        mode='2'  # or '4' depending on the model
    )