import os
import asyncio
import urllib.request
from pyrogram import Client, filters
import ffmpeg
import logging
from pyrogram.errors import MessageNotModified # Import the specific error

# Set up logging for this module
logging.basicConfig(
    level=logging.INFO, # Set the default logging level to INFO
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__) # Get a logger for this module

# --- Configuration for Watermarks ---
# Default image watermark URL
DEFAULT_IMAGE_WATERMARK_URL = "https://i.ibb.co/prXzxGDm/mnbots.jpg"
# Default text watermark
DEFAULT_TEXT_WATERMARK = "join @mnbots in telegram"

# Path for the downloaded default image watermark
DEFAULT_IMAGE_WATERMARK_PATH = "./downloads/default_watermark_image.png"

# Ensure downloads directory exists
os.makedirs("./downloads", exist_ok=True)

# --- Function to download default watermarks on startup/first use ---
async def ensure_default_watermarks():
    """
    Downloads the default image watermark if it doesn't already exist.
    This prevents repeated downloads and ensures the file is available.
    """
    if not os.path.exists(DEFAULT_IMAGE_WATERMARK_PATH):
        try:
            logger.info(f"Downloading default image watermark from {DEFAULT_IMAGE_WATERMARK_URL}...")
            urllib.request.urlretrieve(DEFAULT_IMAGE_WATERMARK_URL, DEFAULT_IMAGE_WATERMARK_PATH)
            logger.info(f"Default image watermark downloaded to {DEFAULT_IMAGE_WATERMARK_PATH}")
        except Exception as e:
            logger.error(f"Failed to download default image watermark from {DEFAULT_IMAGE_WATERMARK_URL}. Error: {e}")
            pass # Continue without image watermark if download fails


# --- Main media handling for videos (direct video or video documents) ---
@Client.on_message((filters.video | filters.document) & filters.private)
async def handle_video_with_watermarks(client, message):
    """
    Handles incoming video messages or video documents in private chats,
    applies dynamically sized photo and text watermarks, then uploads
    the processed video.
    """
    user_id = message.from_user.id
    logger.info(f"Received message from user {user_id} for watermarking.")

    # Determine if it's a direct video or a video document
    input_media = None
    video_width = 0
    video_height = 0
    
    # Supported video file extensions
    video_extensions = {'.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.wmv', '.m4v', '.3gp', '.ts', '.mts'}
    
    if message.video:
        # Direct video message
        input_media = message.video
        video_width = input_media.width or 0
        video_height = input_media.height or 0
        logger.info(f"Processing direct video message")
    elif message.document:
        # Check if it's a video document by MIME type or file extension
        is_video_by_mime = message.document.mime_type and message.document.mime_type.startswith('video/')
        is_video_by_extension = False
        
        if message.document.file_name:
            file_ext = os.path.splitext(message.document.file_name.lower())[1]
            is_video_by_extension = file_ext in video_extensions
        
        if is_video_by_mime or is_video_by_extension:
            input_media = message.document
            # For video documents, dimensions might not be available
            # We'll extract them after download if needed
            video_width = getattr(input_media, 'width', 0) or 0
            video_height = getattr(input_media, 'height', 0) or 0
            logger.info(f"Processing video document: {message.document.file_name}")
        else:
            await message.reply_text("Please send a video file. Supported formats: MP4, MKV, AVI, MOV, WEBM, FLV, WMV, M4V, 3GP, TS, MTS")
            return
    else:
        # Not a video or unsupported file type
        await message.reply_text("Please send a video file (as a direct video or a video document).")
        return

    logger.info(f"Input video dimensions: {video_width}x{video_height} (0 means unknown, will detect after download)")

    # Ensure default watermarks are downloaded before processing
    await ensure_default_watermarks()

    input_file_path = None
    output_file_path = None
    status_message = None

    try:
        status_message = await message.reply_text("Downloading video...")
        
        input_file_path = await message.download(file_name="./downloads/")
        if not input_file_path:
            logger.error(f"Failed to download input video from user {user_id}. Download returned None.")
            await status_message.edit_text("Failed to download the video.")
            return

        logger.info(f"Video downloaded: {input_file_path}")
        
        # If video dimensions are unknown, probe the file to get them
        if video_width == 0 or video_height == 0:
            try:
                probe = ffmpeg.probe(input_file_path)
                video_stream_info = next((s for s in probe['streams'] if s['codec_type'] == 'video'), None)
                if video_stream_info:
                    video_width = video_stream_info.get('width', 720)  # Default fallback
                    video_height = video_stream_info.get('height', 480)  # Default fallback
                    logger.info(f"Detected video dimensions from file: {video_width}x{video_height}")
            except Exception as e:
                logger.warning(f"Could not probe video dimensions, using defaults. Error: {e}")
                video_width, video_height = 720, 480  # Safe defaults
        
        # --- FIX for MessageNotModified error (1/3) ---
        try:
            await status_message.edit_text("Downloaded. Applying watermarks...")
        except MessageNotModified:
            logger.debug("Status message not modified, skipping edit_text.")
            pass # Ignore if message is already identical

        base_name = os.path.basename(input_file_path).rsplit('.', 1)[0]
        output_file_path = f"./downloads/watermarked_{base_name}.mp4"

        # --- FFmpeg Command Construction using chained filters ---
        main_video_input = ffmpeg.input(input_file_path)
        video_stream = main_video_input.video # This will be our continuously transformed video stream
        audio_stream = main_video_input.audio

        # 1. Image Watermark (Top Left, Dynamic Size)
        if os.path.exists(DEFAULT_IMAGE_WATERMARK_PATH):
            image_watermark_input = ffmpeg.input(DEFAULT_IMAGE_WATERMARK_PATH)
            
            # FIX: Use proper scale expression without manual escaping
            # Calculate target width as 10% of video width
            target_width = max(50, int(video_width * 0.1))  # Minimum 50px width
            
            # Process the image watermark stream first
            watermark_processed = (image_watermark_input.video
                                 .filter('scale', target_width, -1)  # Use integers instead of expression
                                 .filter('format', 'rgba')
                                 .filter('colorchannelmixer', aa=0.7))

            # Overlay the processed watermark onto the main video stream
            video_stream = ffmpeg.overlay(video_stream, watermark_processed, x=10, y=10)
            
            logger.info(f"Image watermark configured for top-left position with width {target_width}px.")
        else:
            logger.warning("Default image watermark file not found. Skipping image watermark.")

        # 2. Text Watermark (Bottom Center, Dynamic Font Size)
        text_watermark_content = DEFAULT_TEXT_WATERMARK
        text_opacity = 0.8 # 80% opacity for text
        
        # Calculate font size dynamically based on video height (e.g., 3% of video height)
        dynamic_font_size = max(18, int(video_height * 0.03)) 
        logger.info(f"Calculated text watermark font size: {dynamic_font_size}")

        # FIX: Simplified font file path - try common system fonts
        # First try DejaVu Sans, then fall back to Arial or Liberation Sans
        possible_fonts = [
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
            '/System/Library/Fonts/Arial.ttf',  # macOS
            'C:/Windows/Fonts/arial.ttf'  # Windows
        ]
        
        fontfile_path = None
        for font_path in possible_fonts:
            if os.path.exists(font_path):
                fontfile_path = font_path
                break
        
        if fontfile_path:
            # Apply the drawtext filter to the current video_stream
            video_stream = video_stream.filter('drawtext', 
                                             fontfile=fontfile_path,
                                             text=text_watermark_content,
                                             fontcolor=f'white@{text_opacity}', 
                                             fontsize=dynamic_font_size,
                                             x='(main_w-text_w)/2',  # Use main_w instead of w
                                             y='main_h-text_h-10')   # Use main_h instead of H
            logger.info(f"Text watermark configured with font: {fontfile_path}")
        else:
            # Fallback: use drawtext without fontfile (uses default font)
            video_stream = video_stream.filter('drawtext',
                                             text=text_watermark_content,
                                             fontcolor=f'white@{text_opacity}',
                                             fontsize=dynamic_font_size,
                                             x='(main_w-text_w)/2',
                                             y='main_h-text_h-10')
            logger.info("Text watermark configured with default system font.")

        # Define the final output, using the now fully processed 'video_stream'
        final_output = ffmpeg.output(
            video_stream,
            audio_stream, # Map audio from original input (if exists)
            output_file_path,
            vcodec='libx264',    # Video codec for re-encoding
            acodec='copy',       # Copy audio codec (no re-encoding)
            preset='medium',     # Encoding speed vs. compression efficiency
            crf=26,              # Constant Rate Factor for video quality
            pix_fmt='yuv420p',   # Pixel format for compatibility
            movflags='faststart' # Optimize for web streaming
        )

        logger.info(f"Starting FFmpeg execution for {input_file_path}...")
        try:
            final_output.run(overwrite_output=True, quiet=True)
            logger.info(f"Watermarks applied successfully. Output: {output_file_path}")
        except ffmpeg.Error as e:
            error_message = f"FFmpeg execution failed for {input_file_path}. Stderr: {e.stderr.decode() if e.stderr else 'N/A'}. Error: {str(e)}"
            logger.error(error_message)
            try: # FIX for MessageNotModified error (2/3)
                await status_message.edit_text(f"Error applying watermarks: {error_message}")
            except MessageNotModified:
                logger.debug("Error status message not modified, skipping edit_text.")
                pass
            return

        try: # FIX for MessageNotModified error (3/3)
            await status_message.edit_text("Watermarks applied. Uploading...")
        except MessageNotModified:
            logger.debug("Status message not modified, skipping edit_text.")
            pass # Ignore if message is already identical

        logger.info(f"Uploading processed video: {output_file_path}...")

        # Get metadata of the output video for Pyrogram upload parameters
        try:
            probe = ffmpeg.probe(output_file_path)
            output_video_stream_meta = next((s for s in probe['streams'] if s['codec_type'] == 'video'), None)
            output_duration = int(float(probe['format']['duration'])) if 'duration' in probe['format'] else 0
            output_width = output_video_stream_meta['width'] if output_video_stream_meta else 0
            output_height = output_video_stream_meta['height'] if output_video_stream_meta else 0
        except Exception as e:
            logger.warning(f"Could not probe output video for upload metadata. Error: {e}. Using default values.")
            output_duration, output_width, output_height = 0, 0, 0 # Fallback values

        # Upload the watermarked video to Telegram
        await message.reply_video(
            video=output_file_path,
            caption=f"Watermarked by {DEFAULT_TEXT_WATERMARK} - {base_name}",
            duration=output_duration,
            width=output_width,
            height=output_height,
            supports_streaming=True
        )

        logger.info(f"Watermarked video uploaded successfully for user {user_id}.")
        try:
            await status_message.edit_text("Watermarked video uploaded successfully!")
        except MessageNotModified:
            logger.debug("Final status message not modified, skipping edit_text.")
            pass # Ignore if message is already identical

    except Exception as e:
        logger.exception(f"An unhandled error occurred during video processing for user {user_id}. Error: {e}")
        if status_message:
            try:
                await status_message.edit_text(f"An unexpected error occurred: {e}")
            except MessageNotModified:
                logger.debug("Error status message not modified, skipping edit_text.")
                pass
        else:
            await message.reply_text(f"An unexpected error occurred: {e}")
    finally:
        files_to_clean = [input_file_path, output_file_path]
        for path in files_to_clean:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                    logger.info(f"Cleaned up temporary file: {path}")
                except OSError as e:
                    logger.error(f"Error cleaning up file {path}: {e}")

# Register the handler with the Pyrogram client
def register(app: Client):
    app.add_handler(handle_video_with_watermarks)
