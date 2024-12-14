# Gallery-DL Web Interface

A Docker-based web interface for the [gallery-dl](https://github.com/mikf/gallery-dl) downloader, allowing you to easily download images and videos from various websites through a user-friendly web interface.

## Features

- 🌐 Clean, responsive web interface
- 📊 Real-time download progress updates
- 📁 Automatic file organization
- 📝 Detailed logging
- 📋 Download history and file listing
- 🔄 Persistent downloads across container restarts
- 🚀 Easy deployment with Docker Compose

## Prerequisites

- Docker
- Docker Compose

## Installation & Usage

1. Clone this repository:
   ```bash
   git clone <repository-url>
   cd gallery-dl-web
   ```

2. Build and start the container:
   ```bash
   docker-compose up --build
   ```

3. Access the web interface at `http://localhost:5000`

## Directory Structure

```
.
├── app.py              # Flask application
├── Dockerfile          # Docker configuration
├── docker-compose.yml  # Docker Compose configuration
├── requirements.txt    # Python dependencies
├── templates/         
│   └── index.html     # Web interface template
├── downloads/          # Downloaded files directory (created on first run)
└── logs/              # Application logs (created on first run)
```

## Features in Detail

### Web Interface
- Simple URL input field
- Real-time progress updates
- Download status monitoring
- File listing with sizes and paths

### Download Management
- Automatic directory creation
- File organization by source
- Download progress tracking
- Error handling and reporting

### Logging
- Detailed application logs
- Rotating log files (10MB max size)
- Last 5 log files kept for reference

## Docker Configuration

The application runs in a Docker container with:
- Port 5000 exposed for web access
- Persistent volume for downloads
- Automatic restart unless stopped
- Production-ready with Gunicorn

## Error Handling

- Comprehensive error catching and reporting
- User-friendly error messages
- Detailed logging for troubleshooting
- Graceful failure handling

## Security Notes

- The application runs in a containerized environment
- Downloads are isolated to the downloads directory
- Input validation for URLs
- Error handling prevents command injection

## Contributing

Feel free to submit issues and enhancement requests!

## License

This project is licensed under the MIT License - see the LICENSE file for details.
