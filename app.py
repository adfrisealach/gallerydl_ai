from flask import Flask, render_template, request, jsonify, Response, send_from_directory
import subprocess
import os
import logging
import threading
import queue
import time
import re
import json
import shutil
import signal
import psutil
from logging.handlers import RotatingFileHandler
from urllib.parse import urlparse

app = Flask(__name__)

# Configure logging
if not os.path.exists('logs'):
    os.makedirs('logs')

formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

file_handler = RotatingFileHandler(
    'logs/gallery_dl.log', 
    maxBytes=10485760,  # 10MB
    backupCount=5
)
file_handler.setFormatter(formatter)
file_handler.setLevel(logging.INFO)

# Add debug handler
debug_handler = logging.StreamHandler()
debug_handler.setFormatter(formatter)
debug_handler.setLevel(logging.DEBUG)

app.logger.addHandler(file_handler)
app.logger.addHandler(debug_handler)
app.logger.setLevel(logging.DEBUG)

# Global storage
download_status = {}
message_queues = {}
active_processes = {}  # Store active download processes

def get_folder_name(url):
    """Extract site name and username from URL to create folder name."""
    parsed_url = urlparse(url)
    site = parsed_url.netloc.split('.')[-2]  # Get the main domain name (e.g., instagram from instagram.com)
    
    # Extract username based on the URL pattern
    path_parts = parsed_url.path.strip('/').split('/')
    username = path_parts[0] if path_parts else 'unknown'
    
    # Clean the username to ensure it's filesystem safe
    username = re.sub(r'[^\w\-_]', '_', username)
    
    return f"{site}_{username}"

def validate_url(url):
    """Validate and format URL."""
    if not url:
        return None, None, "URL is required"
        
    # Add https:// if no protocol specified
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
        
    try:
        result = urlparse(url)
        if not all([result.scheme, result.netloc]):
            return None, None, "Invalid URL format"
            
        folder_name = get_folder_name(url)
        return url, folder_name, None
    except Exception:
        return None, None, "Invalid URL format"

def build_gallery_dl_command(url, download_path, options):
    """Build gallery-dl command with filters based on options."""
    command = ['gallery-dl', url, '-D', download_path, '--verbose']
    
    # Add post limit if specified
    if options.get('postLimit') == 'limited':
        post_count = options.get('postCount', 20)
        command.extend(['-c', str(post_count)])
    
    # Add media type filter if specified
    media_type = options.get('mediaType')
    if media_type == 'images':
        command.extend(['--filter', "extension in ['jpg','jpeg','png','gif','webp']"])
    elif media_type == 'videos':
        command.extend(['--filter', "extension in ['mp4','webm','mov']"])
    
    return command

def generate_thumbnail(video_path):
    """Generate a thumbnail for a video file."""
    try:
        import ffmpeg
        thumbnail_path = video_path + '.thumb.jpg'
        
        if os.path.exists(thumbnail_path):
            return
            
        # Extract a frame from the middle of the video
        probe = ffmpeg.probe(video_path)
        duration = float(probe['streams'][0]['duration'])
        time = duration / 2
        
        (
            ffmpeg
            .input(video_path, ss=time)
            .filter('scale', 480, -1)
            .output(thumbnail_path, vframes=1)
            .overwrite_output()
            .run(capture_stdout=True, capture_stderr=True)
        )
    except Exception as e:
        app.logger.error(f"Error generating thumbnail for {video_path}: {str(e)}")

class DownloadTracker:
    def __init__(self):
        self.download_count = 0
        self.scanning = False
        self.has_started = False
        self.last_activity = time.time()
        self.completed = False
        self.stopped = False

def stream_process_output(process, url_id):
    """Stream process output line by line and update status."""
    error_output = []
    tracker = DownloadTracker()
    files_found = set()  # Track unique files found
    files_downloaded = set()  # Track successfully downloaded files
    downloading_started = False
    scanning_complete = False
    
    def send_message(status, message, is_final=False):
        app.logger.debug(f"Sending message - Status: {status}, Message: {message}, Final: {is_final}")
        if url_id in message_queues:  # Check if queue still exists
            message_queues[url_id].put({
                'url_id': url_id,
                'status': status,
                'message': message,
                'is_final': is_final
            })
    
    def handle_process_completion():
        app.logger.debug(f"Process completed - Files found: {len(files_found)}, Downloads: {len(files_downloaded)}")
        
        # Generate thumbnails for any new videos
        for file_path in files_downloaded:
            if any(ext in file_path.lower() for ext in ['.mp4', '.webm', '.mov']):
                generate_thumbnail(file_path)
        
        if tracker.stopped:
            send_message('stopped', 
                f'Download stopped by user. {len(files_downloaded)} file{"s" if len(files_downloaded) != 1 else ""} were downloaded before stopping.', True)
            return
            
        # If we found and downloaded at least one file
        if len(files_downloaded) > 0:
            send_message('completed', 
                f'All identified files retrieved. {len(files_downloaded)} file{"s" if len(files_downloaded) != 1 else ""} downloaded.', True)
            return
            
        # If we found files but didn't download any (they might already exist)
        if len(files_found) > 0:
            send_message('completed', 
                f'All {len(files_found)} files are already downloaded and up to date.', True)
            return
            
        # If we didn't find any files at all
        send_message('error', 'No files were found. Please check if the URL is correct and accessible.', True)
        
        tracker.completed = True
    
    while True:
        if tracker.stopped:
            handle_process_completion()
            break
            
        try:
            line = process.stdout.readline()
            if not line and process.poll() is not None:
                break
                
            if line:
                clean_line = line.strip()
                if clean_line:
                    app.logger.debug(f"Gallery-DL output: {clean_line}")
                    
                    # Track scanning state
                    if "Starting DownloadJob" in clean_line:
                        send_message('scanning', 'Starting download job...')
                        tracker.scanning = True
                        tracker.has_started = True
                    
                    # Detect when scanning is complete (Cursor: None appears)
                    elif "Cursor: None" in clean_line:
                        scanning_complete = True
                        send_message('scanning', 'Finished scanning for files.')
                    
                    # Track files being discovered
                    elif clean_line.startswith('./gallery-dl/') or clean_line.startswith('downloads/'):
                        file_path = clean_line
                        files_found.add(file_path)
                        send_message('scanning', f'Found: {file_path}')
                    
                    # Track actual downloads
                    elif 'Downloaded' in clean_line:
                        downloading_started = True
                        files_downloaded.add(clean_line)  # Add the full download message
                        tracker.last_activity = time.time()
                        send_message('downloading', clean_line)
                        # Send a progress update
                        if len(files_downloaded) % 5 == 0:  # Every 5 downloads
                            send_message('progress', 
                                f'Progress: {len(files_downloaded)} files downloaded so far...')
                    
                    # Other informational messages
                    else:
                        send_message('info', clean_line)
        except (ValueError, IOError) as e:
            if tracker.stopped:
                handle_process_completion()
                break
            app.logger.error(f"Error reading process output: {str(e)}")
            break
    
    try:
        # Process has finished, collect any error output
        error = process.stderr.read()
        if error and not tracker.stopped:  # Only log error if not intentionally stopped
            error_output.append(error.strip())
            app.logger.debug(f"Error output: {error.strip()}")
        
        return_code = process.wait()
        app.logger.debug(f"Process return code: {return_code}")
        
        if tracker.stopped:
            handle_process_completion()
        elif return_code == 0:
            handle_process_completion()
        else:
            if not tracker.stopped:  # Only send error if not intentionally stopped
                error_msg = '\n'.join(error_output) if error_output else 'Download failed with unknown error'
                send_message('error', f'Download failed: {error_msg}', True)
    except Exception as e:
        if not tracker.stopped:  # Only log error if not intentionally stopped
            app.logger.error(f"Error during process completion: {str(e)}")
    finally:
        # Clean up
        if url_id in active_processes:
            del active_processes[url_id]

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/gallery/<source>')
def gallery(source):
    """Serve a gallery view for a specific source."""
    try:
        downloads_path = 'downloads'
        source_path = os.path.join(downloads_path, source)
        
        if not os.path.exists(source_path):
            return "Gallery not found", 404
            
        files = []
        for root, _, filenames in os.walk(source_path):
            for filename in filenames:
                # Skip thumbnail files
                if filename.endswith('.thumb.jpg'):
                    continue
                    
                filepath = os.path.join(root, filename)
                rel_path = os.path.relpath(filepath, downloads_path)
                
                # Get file extension
                _, ext = os.path.splitext(filename)
                ext = ext.lower()
                
                # Determine if it's an image or video
                media_type = 'image' if ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'] else 'video' if ext in ['.mp4', '.webm', '.mov', '.avi', '.mkv'] else 'unknown'
                
                if media_type != 'unknown':
                    file_info = {
                        'name': filename,
                        'path': rel_path,
                        'type': media_type
                    }
                    
                    # Add thumbnail path for videos
                    if media_type == 'video':
                        thumbnail_path = filepath + '.thumb.jpg'
                        if os.path.exists(thumbnail_path):
                            file_info['thumbnail'] = os.path.relpath(thumbnail_path, downloads_path)
                    
                    files.append(file_info)
        
        # Sort files by name
        files.sort(key=lambda x: x['name'])
        return render_template('gallery.html', source=source, files=files)
        
    except Exception as e:
        app.logger.error(f'Error serving gallery: {str(e)}')
        return f"Error: {str(e)}", 500

@app.route('/media/<path:filename>')
def serve_media(filename):
    """Serve media files from the downloads directory."""
    return send_from_directory('downloads', filename)

@app.route('/download', methods=['POST'])
def download():
    data = request.json
    url = data.get('url', '').strip()
    options = {
        'postLimit': data.get('postLimit', 'all'),
        'postCount': data.get('postCount'),
        'mediaType': data.get('mediaType', 'all')
    }
    
    url, folder_name, error = validate_url(url)
    
    if error:
        return jsonify({
            'status': 'error',
            'message': error
        }), 400
    
    # Generate unique ID for this download
    url_id = str(int(time.time()))
    download_status[url_id] = {
        'status': 'starting',
        'messages': []
    }
    
    try:
        # Create base downloads directory if it doesn't exist
        os.makedirs('downloads', exist_ok=True)
        
        # Create specific folder for this download
        download_path = os.path.join('downloads', folder_name)
        os.makedirs(download_path, exist_ok=True)
        
        # Create message queue for this download
        message_queues[url_id] = queue.Queue()
        
        # Build gallery-dl command with options
        command = build_gallery_dl_command(url, download_path, options)
        
        # Run gallery-dl command with real-time output
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            universal_newlines=True,
            preexec_fn=os.setsid  # Create new process group
        )
        
        # Store the process
        active_processes[url_id] = {
            'process': process,
            'tracker': DownloadTracker()
        }
        
        # Start thread to handle process output
        thread = threading.Thread(
            target=stream_process_output,
            args=(process, url_id)
        )
        thread.daemon = True
        thread.start()
        
        app.logger.info(f'Started download for URL: {url} with ID: {url_id} to folder: {folder_name}')
        
        return jsonify({
            'status': 'started',
            'url_id': url_id,
            'message': 'Download started'
        })
            
    except Exception as e:
        error_msg = f'Error starting download: {str(e)}'
        app.logger.error(error_msg)
        return jsonify({
            'status': 'error',
            'message': error_msg
        }), 500

@app.route('/stop/<url_id>', methods=['POST'])
def stop_download(url_id):
    """Stop an active download process."""
    if url_id not in active_processes:
        return jsonify({'error': 'Download not found or already completed'}), 404
        
    try:
        process_info = active_processes[url_id]
        process = process_info['process']
        tracker = process_info['tracker']
        
        # Mark the download as stopped
        tracker.stopped = True
        
        try:
            # Try to terminate the process group
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except OSError:
            # If that fails, try to terminate just the process
            try:
                process.terminate()
            except:
                pass
        
        # Give it a moment to terminate gracefully
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            # If it doesn't terminate gracefully, force kill
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except OSError:
                try:
                    process.kill()
                except:
                    pass
        
        app.logger.info(f'Stopped download process for ID: {url_id}')
        
        # Clean up
        if url_id in message_queues:
            message_queues[url_id].put({
                'url_id': url_id,
                'status': 'stopped',
                'message': 'Download stopped by user',
                'is_final': True
            })
        
        return jsonify({'status': 'success', 'message': 'Download stopped'})
        
    except Exception as e:
        error_msg = f'Error stopping download: {str(e)}'
        app.logger.error(error_msg)
        return jsonify({'error': error_msg}), 500
    finally:
        # Ensure we clean up even if there's an error
        if url_id in active_processes:
            del active_processes[url_id]

@app.route('/status/<url_id>')
def get_status(url_id):
    """Get the current status of a download."""
    if url_id not in download_status:
        return jsonify({'error': 'Download ID not found'}), 404
    
    return jsonify(download_status[url_id])

@app.route('/stream/<url_id>')
def stream(url_id):
    """Stream download progress events."""
    if url_id not in message_queues:
        return jsonify({'error': 'Download ID not found'}), 404

    def generate():
        while True:
            # Check if download is completed, failed, or stopped
            if (url_id in download_status and 
                download_status[url_id]['status'] in ['completed', 'error', 'stopped']):
                break
                
            try:
                message = message_queues[url_id].get(timeout=1)
                # Update status
                if url_id not in download_status:
                    download_status[url_id] = {
                        'status': message['status'],
                        'messages': []
                    }
                
                download_status[url_id]['status'] = message['status']
                download_status[url_id]['messages'].append(message['message'])
                
                # Send the event data as a JSON string
                event_data = {
                    'message': message['message'],
                    'status': message['status'],
                    'is_final': message.get('is_final', False)
                }
                yield f"data: {json.dumps(event_data)}\n\n"
                
                # If this is the final message, clean up
                if message.get('is_final', False):
                    if url_id in message_queues:
                        del message_queues[url_id]
                    break
                    
            except queue.Empty:
                yield f"data: keepalive\n\n"
                
    return Response(generate(), mimetype='text/event-stream')

@app.route('/downloads')
def list_downloads():
    """List all files in the downloads directory."""
    try:
        downloads_path = 'downloads'
        if not os.path.exists(downloads_path):
            return jsonify({'files': []})
            
        files = []
        for root, _, filenames in os.walk(downloads_path):
            for filename in filenames:
                # Skip thumbnail files
                if filename.endswith('.thumb.jpg'):
                    continue
                    
                filepath = os.path.join(root, filename)
                rel_path = os.path.relpath(filepath, downloads_path)
                # Split the path to get site and username
                path_parts = rel_path.split(os.sep)
                source = path_parts[0] if path_parts else 'unknown'
                
                files.append({
                    'name': filename,
                    'path': rel_path,
                    'source': source,
                    'size': os.path.getsize(filepath)
                })
                
        # Sort files by source and name
        files.sort(key=lambda x: (x['source'], x['name']))
        return jsonify({'files': files})
    except Exception as e:
        app.logger.error(f'Error listing downloads: {str(e)}')
        return jsonify({'error': str(e)}), 500

@app.route('/delete/<source>', methods=['DELETE'])
def delete_source(source):
    """Delete a source folder and all its contents."""
    try:
        downloads_path = 'downloads'
        source_path = os.path.join(downloads_path, source)
        
        if not os.path.exists(source_path):
            return jsonify({'error': 'Source not found'}), 404
            
        # Delete the directory and all its contents
        shutil.rmtree(source_path)
        
        app.logger.info(f'Deleted source folder: {source}')
        return jsonify({'status': 'success', 'message': f'Successfully deleted {source}'})
        
    except Exception as e:
        error_msg = f'Error deleting source: {str(e)}'
        app.logger.error(error_msg)
        return jsonify({'error': error_msg}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001)
