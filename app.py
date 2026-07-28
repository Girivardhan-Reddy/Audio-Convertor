#!/usr/bin/env python3
"""
AudioForge Pro - Professional Multi-File Audio Conversion Suite
Production-grade Flask backend with FFmpeg integration
"""

import os
import sys
import json
import uuid
import time
import threading
import logging
import subprocess
import tempfile
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from io import BytesIO
from flask import Flask, render_template, request, jsonify, send_file, abort, make_response
from werkzeug.utils import secure_filename

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 2000 * 1024 * 1024  # 2GB total
app.config['SECRET_KEY'] = os.urandom(24).hex()

# Create temp directories
UPLOAD_DIR = tempfile.mkdtemp(prefix='audioforge_uploads_')
OUTPUT_DIR = tempfile.mkdtemp(prefix='audioforge_outputs_')
BATCH_DIR = tempfile.mkdtemp(prefix='audioforge_batch_')
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(BATCH_DIR, exist_ok=True)

# In-memory storage
conversion_jobs = {}
batch_jobs = {}
conversion_history = []
jobs_lock = threading.Lock()
history_lock = threading.Lock()

# Supported formats and codecs
SUPPORTED_INPUT = {
    'mp3', 'wav', 'aac', 'flac', 'ogg', 'm4a', 'aiff', 'wma', 'ac3', 'opus', 'pcm',
    'mp4', 'mov', 'avi', 'mkv', 'webm'
}

OUTPUT_FORMATS = {
    'wav': {'ext': 'wav', 'label': 'WAV', 'mime': 'audio/wav'},
    'mp3': {'ext': 'mp3', 'label': 'MP3', 'mime': 'audio/mpeg'},
    'aac': {'ext': 'aac', 'label': 'AAC', 'mime': 'audio/aac'},
    'flac': {'ext': 'flac', 'label': 'FLAC', 'mime': 'audio/flac'},
    'aiff': {'ext': 'aiff', 'label': 'AIFF', 'mime': 'audio/aiff'},
    'ogg': {'ext': 'ogg', 'label': 'OGG', 'mime': 'audio/ogg'},
    'opus': {'ext': 'opus', 'label': 'OPUS', 'mime': 'audio/opus'},
}

PCM_CODECS = {
    '16': 'pcm_s16le',
    '24': 'pcm_s24le',
    '32f': 'pcm_f32le',
    '64f': 'pcm_f64le',
}

PRESETS = {
    'netflix': {
        'format': 'wav', 'bit_depth': '32f', 'sample_rate': 48000,
        'channels': 2, 'pcm_codec': 'pcm_f32le', 'label': 'Netflix Delivery'
    },
    'spotify': {
        'format': 'mp3', 'bitrate': '320k', 'sample_rate': 44100,
        'channels': 2, 'label': 'Spotify'
    },
    'apple_music': {
        'format': 'aac', 'bitrate': '256k', 'sample_rate': 44100,
        'channels': 2, 'label': 'Apple Music'
    },
    'youtube': {
        'format': 'aac', 'bitrate': '192k', 'sample_rate': 44100,
        'channels': 2, 'label': 'YouTube'
    },
    'podcast': {
        'format': 'mp3', 'bitrate': '128k', 'sample_rate': 44100,
        'channels': 1, 'label': 'Podcast'
    },
    'cinema': {
        'format': 'wav', 'bit_depth': '24', 'sample_rate': 48000,
        'channels': 6, 'pcm_codec': 'pcm_s24le', 'label': 'Cinema 5.1'
    },
    'broadcast': {
        'format': 'wav', 'bit_depth': '24', 'sample_rate': 48000,
        'channels': 2, 'pcm_codec': 'pcm_s24le', 'label': 'Broadcast'
    },
    'master': {
        'format': 'wav', 'bit_depth': '32f', 'sample_rate': 96000,
        'channels': 2, 'pcm_codec': 'pcm_f32le', 'label': 'Master Archive'
    },
}


def find_ffmpeg():
    """Locate FFmpeg binary"""
    paths = [
        'ffmpeg',
        '/usr/bin/ffmpeg',
        '/usr/local/bin/ffmpeg',
        '/opt/homebrew/bin/ffmpeg',
        'C:\\ffmpeg\\bin\\ffmpeg.exe',
        'C:\\Program Files\\ffmpeg\\bin\\ffmpeg.exe',
    ]
    for path in paths:
        try:
            result = subprocess.run([path, '-version'], capture_output=True, timeout=5)
            if result.returncode == 0:
                logger.info(f"FFmpeg found: {path}")
                return path
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


def find_ffprobe():
    """Locate FFprobe binary"""
    paths = [
        'ffprobe',
        '/usr/bin/ffprobe',
        '/usr/local/bin/ffprobe',
        '/opt/homebrew/bin/ffprobe',
        'C:\\ffmpeg\\bin\\ffprobe.exe',
        'C:\\Program Files\\ffmpeg\\bin\\ffprobe.exe',
    ]
    for path in paths:
        try:
            result = subprocess.run([path, '-version'], capture_output=True, timeout=5)
            if result.returncode == 0:
                logger.info(f"FFprobe found: {path}")
                return path
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


def get_audio_metadata(filepath):
    """Extract audio metadata using ffprobe"""
    ffprobe_path = find_ffprobe()
    if not ffprobe_path:
        return None

    cmd = [
        ffprobe_path,
        '-v', 'quiet',
        '-print_format', 'json',
        '-show_format',
        '-show_streams',
        filepath
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return None
        
        data = json.loads(result.stdout)
        format_info = data.get('format', {})
        
        audio_stream = None
        for stream in data.get('streams', []):
            if stream.get('codec_type') == 'audio':
                audio_stream = stream
                break
        
        if not audio_stream:
            return None
        
        duration = float(format_info.get('duration', 0))
        file_size = int(format_info.get('size', 0))
        
        metadata = {
            'duration': round(duration, 2),
            'duration_formatted': format_duration(duration),
            'file_size': file_size,
            'file_size_formatted': format_size(file_size),
            'format': format_info.get('format_name', '').split(',')[0],
            'codec': audio_stream.get('codec_name', 'unknown'),
            'codec_long': audio_stream.get('codec_long_name', 'unknown'),
            'sample_rate': int(audio_stream.get('sample_rate', 0)),
            'channels': int(audio_stream.get('channels', 0)),
            'channel_layout': audio_stream.get('channel_layout', 'unknown'),
            'bit_rate': int(format_info.get('bit_rate', 0)),
            'bit_depth': audio_stream.get('bits_per_raw_sample', audio_stream.get('bits_per_sample', 0)),
            'bitrate_formatted': f"{int(format_info.get('bit_rate', 0)) // 1000}kbps" if format_info.get('bit_rate') else 'N/A',
        }
        
        return metadata
    except Exception as e:
        logger.error(f"FFprobe error: {e}")
        return None


def format_duration(seconds):
    """Format duration to HH:MM:SS"""
    if not seconds:
        return '00:00:00'
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_size(bytes_val):
    """Format file size"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_val < 1024:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024
    return f"{bytes_val:.1f} TB"


def build_ffmpeg_command(input_path, output_path, params):
    """Build FFmpeg command dynamically based on parameters"""
    ffmpeg_path = find_ffmpeg()
    cmd = [ffmpeg_path, '-y', '-i', input_path]
    
    output_format = params.get('format', 'wav')
    sample_rate = params.get('sample_rate', 48000)
    channels = params.get('channels', 2)
    
    cmd.extend(['-ar', str(sample_rate)])
    cmd.extend(['-ac', str(channels)])
    
    filters = []
    
    if params.get('normalize'):
        filters.append('loudnorm=I=-16:LRA=11:TP=-1.5')
    
    if params.get('noise_reduction'):
        filters.append('afftdn=nr=15:nf=-30')
    
    if params.get('high_pass'):
        freq = params.get('high_pass_freq', 80)
        filters.append(f'highpass=f={freq}')
    
    if params.get('low_pass'):
        freq = params.get('low_pass_freq', 15000)
        filters.append(f'lowpass=f={freq}')
    
    if params.get('compressor'):
        filters.append('compand=attacks=0.1:decays=0.5:points=-80/-80|-30/-15|0/-3|20/-3:gain=3')
    
    if params.get('limiter'):
        filters.append('alimiter=limit=0.95')
    
    if params.get('fade_in'):
        fade_dur = params.get('fade_in_duration', 0.5)
        filters.append(f'afade=t=in:st=0:d={fade_dur}')
    
    if params.get('fade_out'):
        fade_dur = params.get('fade_out_duration', 0.5)
        filters.append(f'afade=t=out:st=999999:d={fade_dur}')
    
    if filters:
        cmd.extend(['-af', ','.join(filters)])
    
    if output_format == 'wav':
        bit_depth = params.get('bit_depth', '16')
        codec = PCM_CODECS.get(bit_depth, 'pcm_s16le')
        cmd.extend(['-c:a', codec])
    elif output_format == 'mp3':
        bitrate = params.get('bitrate', '192k')
        cmd.extend(['-c:a', 'libmp3lame', '-b:a', bitrate])
    elif output_format == 'aac':
        bitrate = params.get('bitrate', '192k')
        cmd.extend(['-c:a', 'aac', '-b:a', bitrate])
    elif output_format == 'flac':
        cmd.extend(['-c:a', 'flac', '-compression_level', '8'])
    elif output_format == 'aiff':
        bit_depth = params.get('bit_depth', '16')
        codec = PCM_CODECS.get(bit_depth, 'pcm_s16be')
        cmd.extend(['-c:a', codec])
    elif output_format == 'ogg':
        bitrate = params.get('bitrate', '192k')
        cmd.extend(['-c:a', 'libvorbis', '-b:a', bitrate])
    elif output_format == 'opus':
        bitrate = params.get('bitrate', '128k')
        cmd.extend(['-c:a', 'libopus', '-b:a', bitrate])
    
    cmd.extend(['-progress', 'pipe:1', '-nostats'])
    cmd.append(output_path)
    
    return cmd


def convert_single_file(input_path, output_path, params, progress_callback=None):
    """Convert a single file and return success status"""
    try:
        metadata = get_audio_metadata(input_path)
        total_duration = metadata.get('duration', 0) if metadata else 0
        
        cmd = build_ffmpeg_command(input_path, output_path, params)
        logger.info(f"FFmpeg command: {' '.join(cmd)}")
        
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            universal_newlines=True
        )
        
        while True:
            line = process.stdout.readline()
            if not line and process.poll() is not None:
                break
            
            if 'out_time_ms=' in line and total_duration > 0 and progress_callback:
                try:
                    time_ms = int(line.split('=')[1].strip())
                    current_time = time_ms / 1_000_000
                    progress = min(int((current_time / total_duration) * 100), 99)
                    progress_callback(progress)
                except (ValueError, IndexError):
                    pass
        
        process.wait()
        
        if process.returncode != 0:
            stderr_output = process.stderr.read()
            logger.error(f"FFmpeg error: {stderr_output}")
            return False, stderr_output
        
        return True, None
        
    except Exception as e:
        logger.error(f"Conversion error: {e}", exc_info=True)
        return False, str(e)


def process_batch_conversion(batch_id, file_list, output_dir, params):
    """Execute batch conversion in background thread"""
    global conversion_history
    
    try:
        with jobs_lock:
            batch_jobs[batch_id]['status'] = 'processing'
            batch_jobs[batch_id]['progress'] = 0
        
        total_files = len(file_list)
        completed_files = 0
        failed_files = []
        output_files = []
        
        for idx, file_info in enumerate(file_list):
            input_path = file_info['path']
            original_name = file_info['original_name']
            base_name = os.path.splitext(original_name)[0]
            output_format = params.get('format', 'wav')
            output_filename = f"{base_name}.{output_format}"
            output_path = os.path.join(output_dir, output_filename)
            
            # Create a progress callback for this file
            def make_callback(file_index, total):
                def callback(progress):
                    overall_progress = int(((file_index + progress / 100) / total) * 100)
                    with jobs_lock:
                        if batch_id in batch_jobs:
                            batch_jobs[batch_id]['progress'] = min(overall_progress, 99)
                            batch_jobs[batch_id]['current_file'] = file_index + 1
                            batch_jobs[batch_id]['current_file_name'] = original_name
                return callback
            
            file_callback = make_callback(idx, total_files)
            
            # Convert the file
            success, error = convert_single_file(input_path, output_path, params, file_callback)
            
            if success:
                completed_files += 1
                output_size = os.path.getsize(output_path)
                output_files.append({
                    'original_name': original_name,
                    'output_name': output_filename,
                    'output_path': output_path,
                    'size': output_size,
                    'size_formatted': format_size(output_size),
                })
            else:
                failed_files.append({
                    'original_name': original_name,
                    'error': error or 'Unknown error',
                })
        
        # Create ZIP file containing all converted files
        zip_filename = f"audioforge_batch_{batch_id[:8]}.zip"
        zip_path = os.path.join(OUTPUT_DIR, zip_filename)
        
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for file_info in output_files:
                zf.write(file_info['output_path'], file_info['output_name'])
        
        zip_size = os.path.getsize(zip_path)
        download_url = f"/api/download/batch/{batch_id}"
        
        with jobs_lock:
            batch_jobs[batch_id]['status'] = 'completed'
            batch_jobs[batch_id]['progress'] = 100
            batch_jobs[batch_id]['completed_files'] = completed_files
            batch_jobs[batch_id]['failed_files'] = failed_files
            batch_jobs[batch_id]['output_files'] = output_files
            batch_jobs[batch_id]['zip_path'] = zip_path
            batch_jobs[batch_id]['zip_size'] = zip_size
            batch_jobs[batch_id]['zip_size_formatted'] = format_size(zip_size)
            batch_jobs[batch_id]['download_url'] = download_url
        
        # Add to history
        history_entry = {
            'id': batch_id,
            'type': 'batch',
            'input_files': len(file_list),
            'completed_files': completed_files,
            'output_format': params.get('format', 'wav').upper(),
            'timestamp': datetime.now().isoformat(),
            'download_url': download_url,
            'total_size': zip_size,
        }
        
        with history_lock:
            conversion_history.insert(0, history_entry)
            if len(conversion_history) > 50:
                conversion_history = conversion_history[:50]
        
        logger.info(f"Batch conversion complete: {batch_id}, {completed_files}/{total_files} files")
        
    except Exception as e:
        logger.error(f"Batch conversion error: {e}", exc_info=True)
        with jobs_lock:
            if batch_id in batch_jobs:
                batch_jobs[batch_id]['status'] = 'error'
                batch_jobs[batch_id]['error'] = str(e)


def process_single_conversion(job_id, input_path, output_path, params):
    """Execute single file conversion in background thread"""
    global conversion_history
    
    try:
        with jobs_lock:
            job = conversion_jobs.get(job_id)
            if not job:
                return
            job['status'] = 'processing'
            job['progress'] = 0
        
        metadata = get_audio_metadata(input_path)
        total_duration = metadata.get('duration', 0) if metadata else 0
        
        def progress_callback(progress):
            with jobs_lock:
                if job_id in conversion_jobs:
                    conversion_jobs[job_id]['progress'] = progress
        
        success, error = convert_single_file(input_path, output_path, params, progress_callback)
        
        if not success:
            with jobs_lock:
                if job_id in conversion_jobs:
                    conversion_jobs[job_id]['status'] = 'error'
                    conversion_jobs[job_id]['error'] = error or 'Unknown error'
            return
        
        output_size = os.path.getsize(output_path)
        output_metadata = get_audio_metadata(output_path)
        original_name = job.get('original_name', 'audio')
        base_name = os.path.splitext(original_name)[0]
        output_format = params.get('format', 'wav')
        output_filename = f"{base_name}.{output_format}"
        
        download_url = f"/api/download/{job_id}"
        
        with jobs_lock:
            if job_id in conversion_jobs:
                job = conversion_jobs[job_id]
                job['status'] = 'completed'
                job['progress'] = 100
                job['output_path'] = output_path
                job['output_size'] = output_size
                job['output_size_formatted'] = format_size(output_size)
                job['download_url'] = download_url
                job['output_filename'] = output_filename
                job['output_metadata'] = output_metadata
        
        history_entry = {
            'id': job_id,
            'type': 'single',
            'input_file': os.path.basename(input_path),
            'output_format': params.get('format', 'wav').upper(),
            'timestamp': datetime.now().isoformat(),
            'duration': total_duration,
            'size': output_size,
            'download_url': download_url,
        }
        
        with history_lock:
            conversion_history.insert(0, history_entry)
            if len(conversion_history) > 50:
                conversion_history = conversion_history[:50]
        
        logger.info(f"Single conversion complete: {job_id}")
        
    except Exception as e:
        logger.error(f"Conversion error: {e}", exc_info=True)
        with jobs_lock:
            if job_id in conversion_jobs:
                conversion_jobs[job_id]['status'] = 'error'
                conversion_jobs[job_id]['error'] = str(e)


@app.route('/')
def index():
    """Main page"""
    return render_template('index.html')


@app.route('/api/upload', methods=['POST'])
def upload_files():
    """Handle multiple file upload with metadata extraction"""
    if 'files' not in request.files:
        # Try single file upload for backward compatibility
        if 'file' in request.files:
            file = request.files['file']
            return handle_single_upload(file)
        return jsonify({'error': 'No files provided'}), 400
    
    files = request.files.getlist('files')
    if not files or len(files) == 0:
        return jsonify({'error': 'No files selected'}), 400
    
    uploaded_files = []
    total_size = 0
    
    for file in files:
        if not file.filename:
            continue
        
        ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
        if ext not in SUPPORTED_INPUT:
            continue
        
        safe_name = secure_filename(file.filename)
        unique_name = f"{uuid.uuid4().hex}_{safe_name}"
        filepath = os.path.join(UPLOAD_DIR, unique_name)
        file.save(filepath)
        
        metadata = get_audio_metadata(filepath)
        
        if metadata:
            total_size += metadata['file_size']
            uploaded_files.append({
                'filename': unique_name,
                'original_name': safe_name,
                'file_size': metadata['file_size'],
                'file_size_formatted': metadata['file_size_formatted'],
                'duration': metadata['duration'],
                'duration_formatted': metadata['duration_formatted'],
                'format': metadata['format'],
                'codec': metadata['codec'],
                'codec_long': metadata['codec_long'],
                'sample_rate': metadata['sample_rate'],
                'channels': metadata['channels'],
                'bit_rate': metadata['bit_rate'],
                'bit_depth': metadata['bit_depth'],
                'bitrate_formatted': metadata['bitrate_formatted'],
            })
        else:
            os.remove(filepath)
    
    if not uploaded_files:
        return jsonify({'error': 'No valid audio files found'}), 400
    
    return jsonify({
        'success': True,
        'files': uploaded_files,
        'total_files': len(uploaded_files),
        'total_size': total_size,
        'total_size_formatted': format_size(total_size),
    })


def handle_single_upload(file):
    """Handle single file upload"""
    if not file.filename:
        return jsonify({'error': 'Empty filename'}), 400
    
    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in SUPPORTED_INPUT:
        return jsonify({'error': f'Unsupported format: {ext}'}), 400
    
    safe_name = secure_filename(file.filename)
    unique_name = f"{uuid.uuid4().hex}_{safe_name}"
    filepath = os.path.join(UPLOAD_DIR, unique_name)
    file.save(filepath)
    
    metadata = get_audio_metadata(filepath)
    
    if not metadata:
        os.remove(filepath)
        return jsonify({'error': 'Unable to read audio file'}), 400
    
    return jsonify({
        'success': True,
        'filename': unique_name,
        'original_name': safe_name,
        'file_size': metadata['file_size'],
        'file_size_formatted': metadata['file_size_formatted'],
        'duration': metadata['duration'],
        'duration_formatted': metadata['duration_formatted'],
        'format': metadata['format'],
        'codec': metadata['codec'],
        'codec_long': metadata['codec_long'],
        'sample_rate': metadata['sample_rate'],
        'channels': metadata['channels'],
        'channel_layout': metadata['channel_layout'],
        'bit_rate': metadata['bit_rate'],
        'bit_depth': metadata['bit_depth'],
        'bitrate_formatted': metadata['bitrate_formatted'],
    })


@app.route('/api/convert/single', methods=['POST'])
def start_single_conversion():
    """Start single file conversion"""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No parameters provided'}), 400
    
    input_filename = data.get('filename')
    if not input_filename:
        return jsonify({'error': 'No input file specified'}), 400
    
    input_path = os.path.join(UPLOAD_DIR, input_filename)
    if not os.path.exists(input_path):
        return jsonify({'error': 'Uploaded file not found'}), 404
    
    job_id = uuid.uuid4().hex
    output_format = data.get('format', 'wav')
    output_filename = f"{job_id}.{output_format}"
    output_path = os.path.join(OUTPUT_DIR, output_filename)
    
    download_url = f"/api/download/{job_id}"
    
    # Get original name for output filename
    original_name = data.get('original_name', 'audio')
    
    with jobs_lock:
        conversion_jobs[job_id] = {
            'id': job_id,
            'status': 'queued',
            'progress': 0,
            'input_path': input_path,
            'output_path': output_path,
            'params': data,
            'original_name': original_name,
            'created_at': time.time(),
            'download_url': download_url,
            'type': 'single',
        }
    
    thread = threading.Thread(
        target=process_single_conversion,
        args=(job_id, input_path, output_path, data),
        daemon=True
    )
    thread.start()
    
    return jsonify({
        'success': True,
        'job_id': job_id,
        'download_url': download_url,
        'status': 'queued',
    })


@app.route('/api/convert/batch', methods=['POST'])
def start_batch_conversion():
    """Start batch file conversion"""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No parameters provided'}), 400
    
    files_data = data.get('files', [])
    if not files_data:
        return jsonify({'error': 'No files specified'}), 400
    
    # Create batch job
    batch_id = uuid.uuid4().hex
    batch_output_dir = os.path.join(BATCH_DIR, batch_id)
    os.makedirs(batch_output_dir, exist_ok=True)
    
    # Prepare file list
    file_list = []
    for file_info in files_data:
        input_path = os.path.join(UPLOAD_DIR, file_info['filename'])
        if os.path.exists(input_path):
            file_list.append({
                'path': input_path,
                'original_name': file_info.get('original_name', 'audio'),
            })
    
    if not file_list:
        return jsonify({'error': 'No valid files found'}), 400
    
    # Store batch job
    download_url = f"/api/download/batch/{batch_id}"
    
    with jobs_lock:
        batch_jobs[batch_id] = {
            'id': batch_id,
            'status': 'queued',
            'progress': 0,
            'total_files': len(file_list),
            'completed_files': 0,
            'current_file': 1,
            'current_file_name': file_list[0]['original_name'] if file_list else '',
            'params': data,
            'created_at': time.time(),
            'download_url': download_url,
            'type': 'batch',
        }
    
    thread = threading.Thread(
        target=process_batch_conversion,
        args=(batch_id, file_list, batch_output_dir, data),
        daemon=True
    )
    thread.start()
    
    return jsonify({
        'success': True,
        'batch_id': batch_id,
        'download_url': download_url,
        'total_files': len(file_list),
        'status': 'queued',
    })


@app.route('/api/convert', methods=['POST'])
def start_conversion():
    """Start conversion - routes to single or batch based on data"""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No parameters provided'}), 400
    
    # Check if batch conversion
    if 'files' in data and isinstance(data['files'], list) and len(data['files']) > 0:
        return start_batch_conversion()
    elif 'filename' in data:
        return start_single_conversion()
    else:
        return jsonify({'error': 'No files specified'}), 400


@app.route('/api/progress/<job_id>')
def get_progress(job_id):
    """Get conversion progress (single or batch)"""
    with jobs_lock:
        job = conversion_jobs.get(job_id) or batch_jobs.get(job_id)
    
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    
    response = {
        'job_id': job_id,
        'status': job.get('status', 'unknown'),
        'progress': job.get('progress', 0),
        'type': job.get('type', 'single'),
    }
    
    if job.get('type') == 'batch':
        response.update({
            'total_files': job.get('total_files', 0),
            'completed_files': job.get('completed_files', 0),
            'current_file': job.get('current_file', 1),
            'current_file_name': job.get('current_file_name', ''),
        })
    
    if job.get('status') == 'completed':
        response.update({
            'download_url': job.get('download_url', ''),
            'output_filename': job.get('output_filename', ''),
            'output_size': job.get('output_size_formatted', job.get('zip_size_formatted', '')),
            'output_metadata': job.get('output_metadata', {}),
        })
        
        if job.get('type') == 'batch':
            response['failed_files'] = job.get('failed_files', [])
            response['output_files'] = job.get('output_files', [])
    
    if job.get('status') == 'error':
        response['error'] = job.get('error', 'Unknown error')
    
    return jsonify(response)


@app.route('/api/download/<job_id>')
def download_file(job_id):
    """Download single converted file"""
    with jobs_lock:
        job = conversion_jobs.get(job_id)
    
    if not job:
        abort(404, description="Job not found")
    
    if job.get('status') != 'completed':
        abort(400, description="Conversion not complete")
    
    output_path = job.get('output_path')
    if not output_path or not os.path.exists(output_path):
        abort(404, description="Output file not found")
    
    download_name = job.get('output_filename', f"audioforge_converted.{job.get('params', {}).get('format', 'wav')}")
    
    return send_file(
        output_path,
        as_attachment=True,
        download_name=download_name,
        mimetype=OUTPUT_FORMATS.get(job.get('params', {}).get('format', 'wav'), {}).get('mime', 'application/octet-stream')
    )


@app.route('/api/download/batch/<batch_id>')
def download_batch(batch_id):
    """Download batch conversion as ZIP"""
    with jobs_lock:
        job = batch_jobs.get(batch_id)
    
    if not job:
        abort(404, description="Batch job not found")
    
    if job.get('status') != 'completed':
        abort(400, description="Batch conversion not complete")
    
    zip_path = job.get('zip_path')
    if not zip_path or not os.path.exists(zip_path):
        abort(404, description="Batch output not found")
    
    return send_file(
        zip_path,
        as_attachment=True,
        download_name=f"audioforge_batch_converted.zip",
        mimetype='application/zip'
    )


@app.route('/api/audio/<filename>')
def serve_audio(filename):
    """Serve uploaded audio file for waveform preview"""
    safe_name = secure_filename(filename)
    filepath = os.path.join(UPLOAD_DIR, safe_name)
    
    if not os.path.exists(filepath):
        abort(404, description="File not found")
    
    ext = safe_name.rsplit('.', 1)[-1].lower() if '.' in safe_name else ''
    mime_map = {
        'mp3': 'audio/mpeg', 'wav': 'audio/wav', 'aac': 'audio/aac',
        'flac': 'audio/flac', 'ogg': 'audio/ogg', 'opus': 'audio/ogg',
        'aiff': 'audio/aiff', 'm4a': 'audio/mp4',
    }
    
    return send_file(filepath, mimetype=mime_map.get(ext, 'audio/mpeg'))


@app.route('/api/metadata/<filename>')
def get_metadata(filename):
    """Get metadata for uploaded file"""
    filepath = os.path.join(UPLOAD_DIR, secure_filename(filename))
    if not os.path.exists(filepath):
        return jsonify({'error': 'File not found'}), 404
    
    metadata = get_audio_metadata(filepath)
    if not metadata:
        return jsonify({'error': 'Unable to read metadata'}), 500
    
    return jsonify(metadata)


@app.route('/api/history')
def get_history():
    """Get conversion history"""
    global conversion_history
    with history_lock:
        return jsonify(list(conversion_history))


@app.route('/api/presets')
def get_presets():
    """Get available presets"""
    return jsonify(PRESETS)


@app.route('/api/ffmpeg-info')
def get_ffmpeg_info():
    """Get FFmpeg version info"""
    ffmpeg_path = find_ffmpeg()
    ffprobe_path = find_ffprobe()
    
    info = {
        'ffmpeg_found': ffmpeg_path is not None,
        'ffprobe_found': ffprobe_path is not None,
    }
    
    if ffmpeg_path:
        try:
            result = subprocess.run([ffmpeg_path, '-version'], capture_output=True, text=True, timeout=5)
            info['ffmpeg_version'] = result.stdout.split('\n')[0] if result.stdout else 'Unknown'
            info['ffmpeg_path'] = ffmpeg_path
        except Exception:
            info['ffmpeg_version'] = 'Error getting version'
    
    return jsonify(info)


@app.route('/api/command-preview', methods=['POST'])
def command_preview():
    """Preview FFmpeg command without executing"""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No parameters'}), 400
    
    input_path = '/path/to/input.file'
    output_path = f"/path/to/output.{data.get('format', 'wav')}"
    
    cmd = build_ffmpeg_command(input_path, output_path, data)
    
    return jsonify({'command': ' '.join(cmd), 'raw': cmd})


@app.route('/api/cleanup', methods=['POST'])
def cleanup_temp():
    """Clean temporary files"""
    try:
        shutil.rmtree(UPLOAD_DIR, ignore_errors=True)
        shutil.rmtree(OUTPUT_DIR, ignore_errors=True)
        shutil.rmtree(BATCH_DIR, ignore_errors=True)
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        os.makedirs(BATCH_DIR, exist_ok=True)
        return jsonify({'success': True, 'message': 'Temporary files cleaned'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.errorhandler(413)
def too_large(e):
    return jsonify({'error': 'File too large. Maximum 2GB total allowed.'}), 413


@app.errorhandler(404)
def not_found(e):
    return jsonify({'error': 'Resource not found'}), 404


@app.errorhandler(500)
def server_error(e):
    return jsonify({'error': 'Internal server error'}), 500


if __name__ == '__main__':
    ffmpeg_path = find_ffmpeg()
    ffprobe_path = find_ffprobe()
    
    if not ffmpeg_path:
        logger.warning("=" * 60)
        logger.warning("FFMPEG NOT FOUND!")
        logger.warning("Please install FFmpeg and ensure it's in your PATH")
        logger.warning("=" * 60)
    
    logger.info("Starting AudioForge Pro server...")
    app.run(debug=True, host='0.0.0.0', port=5000, threaded=True)