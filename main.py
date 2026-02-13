import os
import json
import subprocess
import tempfile
import shutil
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, HttpUrl
from typing import Optional, List, Dict, Any
import uuid
import asyncio
import re

app = FastAPI(title="Universal Video Downloader API", version="2.0.0")

# Configure CORS - Allow your Vercel frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        # "http://localhost:3000",
        "https://all-video-downloader-two.vercel.app"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request/Response Models
class DownloadRequest(BaseModel):
    videoUrl: str
    quality: Optional[str] = "best"

class InfoResponse(BaseModel):
    title: str
    duration: Optional[int]
    uploader: Optional[str]
    upload_date: Optional[str]
    view_count: Optional[int]
    like_count: Optional[int]
    comment_count: Optional[int]
    platform: str
    thumbnail: Optional[str]
    formats_available: int
    filesize_approx: Optional[int]
    description: Optional[str] = ""

class ErrorResponse(BaseModel):
    error: str
    details: Optional[str] = None
    platform: Optional[str] = None

# Utility Functions
def sanitize_filename(filename: str) -> str:
    """Remove invalid characters from filename"""
    filename = re.sub(r'[^\w\s-]', '_', filename)
    filename = re.sub(r'\s+', '_', filename)
    return filename[:200]  # Limit filename length

def format_duration(seconds: Optional[int]) -> str:
    """Convert seconds to human readable format"""
    if not seconds:
        return "Unknown"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if secs > 0 or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)

async def stream_yt_dlp(url: str, quality: str = "best"):
    """Stream yt-dlp output as a generator"""
    cmd = [
        'yt-dlp',
        url,
        '-f', f'{quality}[ext=mp4]/best[ext=mp4]/best',
        '-o', '-',
        '--no-playlist',
        '--no-warnings',
        '--no-call-home',
        '--no-check-certificate',
        '--prefer-free-formats',
    ]
    
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    
    while True:
        chunk = await process.stdout.read(8192)
        if not chunk:
            break
        yield chunk
    
    await process.wait()

# Health Check
@app.get("/")
async def root():
    return {
        "service": "Universal Video Downloader API",
        "status": "operational",
        "version": "2.0.0",
        "endpoints": {
            "/info": "POST - Get video information",
            "/download": "POST - Download video",
            "/health": "GET - Health check"
        }
    }

@app.get("/health")
async def health_check():
    """Health check endpoint for Railway"""
    try:
        # Check if yt-dlp is installed
        result = subprocess.run(['yt-dlp', '--version'], capture_output=True, text=True)
        yt_dlp_version = result.stdout.strip()
    except:
        yt_dlp_version = "not found"
    
    return {
        "status": "healthy",
        "yt-dlp_version": yt_dlp_version,
        "python_version": os.sys.version
    }

# Get Video Information
@app.post("/info", response_model=InfoResponse)
async def get_video_info(request: DownloadRequest):
    """Get video information without downloading"""
    try:
        # Run yt-dlp to get JSON info
        cmd = [
            'yt-dlp',
            request.videoUrl,
            '--dump-json',
            '--no-playlist',
            '--no-warnings',
            '--no-call-home',
            '--no-check-certificate'
        ]
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            error_msg = stderr.decode().strip()
            raise HTTPException(status_code=400, detail=f"yt-dlp error: {error_msg}")
        
        # Parse JSON output
        info = json.loads(stdout)
        
        # Get thumbnail URL
        thumbnail = None
        if info.get('thumbnails'):
            thumbnails = info['thumbnails']
            if thumbnails:
                thumbnail = thumbnails[-1].get('url')
        elif info.get('thumbnail'):
            thumbnail = info['thumbnail']
        
        # Clean description
        description = info.get('description', '')
        if description and len(description) > 500:
            description = description[:500] + "..."
        
        return InfoResponse(
            title=info.get('title', 'Unknown Title'),
            duration=info.get('duration'),
            uploader=info.get('uploader') or info.get('channel') or info.get('creator'),
            upload_date=info.get('upload_date'),
            view_count=info.get('view_count'),
            like_count=info.get('like_count'),
            comment_count=info.get('comment_count'),
            platform=info.get('extractor_key', 'Unknown'),
            thumbnail=thumbnail,
            formats_available=len(info.get('formats', [])),
            filesize_approx=info.get('filesize_approx') or info.get('filesize'),
            description=description
        )
        
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Failed to parse video information")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Download Video
@app.post("/download")
async def download_video(request: DownloadRequest):
    """Stream video download"""
    try:
        # Get video info first for filename
        cmd_info = [
            'yt-dlp',
            request.videoUrl,
            '--dump-json',
            '--no-playlist',
            '--no-warnings',
            '--no-call-home',
            '--no-check-certificate'
        ]
        
        process_info = await asyncio.create_subprocess_exec(
            *cmd_info,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout_info, stderr_info = await process_info.communicate()
        
        if process_info.returncode != 0:
            error_msg = stderr_info.decode().strip()
            raise HTTPException(status_code=400, detail=f"yt-dlp error: {error_msg}")
        
        info = json.loads(stdout_info)
        title = sanitize_filename(info.get('title', 'video'))
        ext = info.get('ext', 'mp4')
        
        # Determine quality
        quality = request.quality
        if quality not in ['best', 'worst', '720p', '480p', '360p']:
            quality = 'best'
        
        # Map quality to yt-dlp format
        format_map = {
            'best': 'best[ext=mp4]/best',
            'worst': 'worst[ext=mp4]/worst',
            '720p': 'best[height<=720][ext=mp4]/best[height<=720]',
            '480p': 'best[height<=480][ext=mp4]/best[height<=480]',
            '360p': 'best[height<=360][ext=mp4]/best[height<=360]'
        }
        
        format_spec = format_map.get(quality, 'best[ext=mp4]/best')
        
        # Stream the video
        stream_generator = stream_yt_dlp(request.videoUrl, format_spec)
        
        filename = f"{title}.mp4"
        
        return StreamingResponse(
            stream_generator,
            media_type="video/mp4",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Access-Control-Expose-Headers": "Content-Disposition"
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Download with specific format
@app.post("/download/format")
async def download_format(request: DownloadRequest, format_id: str):
    """Download with specific format ID"""
    try:
        cmd_info = [
            'yt-dlp',
            request.videoUrl,
            '--dump-json',
            '--no-playlist',
            '--no-warnings'
        ]
        
        process_info = await asyncio.create_subprocess_exec(
            *cmd_info,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout_info, _ = await process_info.communicate()
        info = json.loads(stdout_info)
        title = sanitize_filename(info.get('title', 'video'))
        
        # Stream with specific format
        cmd = [
            'yt-dlp',
            request.videoUrl,
            '-f', format_id,
            '-o', '-',
            '--no-playlist',
            '--no-warnings'
        ]
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        async def stream_output():
            while True:
                chunk = await process.stdout.read(8192)
                if not chunk:
                    break
                yield chunk
        
        return StreamingResponse(
            stream_output(),
            media_type="video/mp4",
            headers={
                "Content-Disposition": f'attachment; filename="{title}.mp4"'
            }
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Get available formats
@app.post("/formats")
async def get_formats(request: DownloadRequest):
    """Get all available formats for a video"""
    try:
        cmd = [
            'yt-dlp',
            request.videoUrl,
            '--dump-json',
            '--no-playlist',
            '--no-warnings'
        ]
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        info = json.loads(stdout)
        
        formats = []
        for f in info.get('formats', []):
            if f.get('vcodec') != 'none' or f.get('acodec') != 'none':
                formats.append({
                    'format_id': f.get('format_id'),
                    'ext': f.get('ext'),
                    'quality': f.get('format_note') or f.get('quality') or 'Unknown',
                    'filesize': f.get('filesize') or f.get('filesize_approx'),
                    'vcodec': f.get('vcodec'),
                    'acodec': f.get('acodec'),
                    'height': f.get('height'),
                    'width': f.get('width'),
                    'fps': f.get('fps')
                })
        
        return {
            'title': info.get('title'),
            'formats': formats,
            'format_count': len(formats)
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
