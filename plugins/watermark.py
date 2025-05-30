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
# Default image watermark URL (NEW URL as provided)
DEFAULT_IMAGE_WATERMARK_URL = "https://i.ibb.co/prXzxGDm/mnbots.jpg"
# Default text watermark (Your provided text)
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
    if message.video:
        input_media = message.video
    elif message.document and message.document.mime_type and message.document.mime_type.startswith('video/'):
        input_media = message.document.video # message.document.video holds video metadata for document
    else:
        # Not a video or unsupported file type, ignore
        await message.reply_text("Please send a video file (as a direct video or a video document).")
        return

    # Extract video dimensions for dynamic watermark sizing
    video_width = input_media.width
    video_height = input_media.height
    logger.info(f"Input video dimensions: {video_width}x{video_height}")

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
        
        # --- FIX for MessageNotModified error (1/3) ---
        try:
            await status_message.edit_text("Downloaded. Applying watermarks...")
        except MessageNotModified:
            logger.debug("Status message not modified, skipping edit_text.")
            pass # Ignore if message is already identical

        base_name = os.path.basename(input_file_path).rsplit('.', 1)[0]
        output_file_path = f"./downloads/watermarked_{base_name}.mp4"

        # --- FFmpeg Command Construction using complex_filter ---
        main_video_input = ffmpeg.input(input_file_path)
        
        # Get separate input for the image watermark
        image_watermark_input = ffmpeg.input(DEFAULT_IMAGE_WATERMARK_PATH)

        # Build the filtergraph string manually
        filter_graph_commands = []
        
        # Base stream (the input video)
        video_stream = main_video_input.video
        audio_stream = main_video_input.audio

        # 1. Image Watermark (Top Left, Dynamic Size)
        # We need to scale the image watermark first, then overlay it.
        # This uses FFmpeg's named pads for clarity in complex filtergraphs.
        
        if os.path.exists(DEFAULT_IMAGE_WATERMARK_PATH):
            # Define the scale for the image watermark (10% of video width)
            watermark_scale_expr = 'iw*0.1:-1' # <--- THIS LINE IS NOW CORRECTLY PLACED AND DEFINED
            
            # Define the position for the overlay
            overlay_x = 10
            overlay_y = 10
            
            # Define input streams as references (e.g., [0:v] for main video, [1:v] for image watermark)
            video_input_ref = main_video_input.video
            image_input_ref = image_watermark_input.video

            # Apply scale, format, and colorchannelmixer to the image watermark stream
            image_watermark_processing_stream = image_input_ref.filter_('scale', watermark_scale_expr).filter_('format', 'rgba').filter_('colorchannelmixer', aa=0.7)

            # Overlay the processed watermark onto the main video stream
            video_stream_with_image_wm = ffmpeg.overlay(video_input_ref, image_watermark_processing_stream, x=overlay_x, y=overlay_y)
            
            # Now, this combined stream will be the input for the next filter (drawtext)
            video_stream = video_stream_with_image_wm

            logger.info("Image watermark configured for top-left position with dynamic scaling via complex_filter.")
        else:
            logger.warning("Default image watermark file not found. Skipping image watermark.")

        # 2. Text Watermark (Bottom Center, Dynamic Font Size)
        text_watermark_content = DEFAULT_TEXT_WATERMARK
        text_opacity = 0.8 # 80% opacity for text
        
        # Calculate font size dynamically based on video height (e.g., 3% of video height)
        # Ensure it's an integer and has a minimum size to be readable
        dynamic_font_size = max(18, int(video_height * 0.03)) 
        logger.info(f"Calculated text watermark font size: {dynamic_font_size}")

        # The fontfile path requires proper escaping for FFmpeg's drawtext filter
        fontfile_escaped = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'.replace(':', '\\:')

        # Apply the drawtext filter
        video_stream = video_stream.filter('drawtext', fontfile=fontfile_escaped, text=text_watermark_content,
                                            fontcolor=f'white@{text_opacity}', fontsize=dynamic_font_size,
                                            x='(w-text_w)/2', y='H-text_h-10')

        logger.info("Text watermark configured for bottom-center position with dynamic font size.")


        # Define the output
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
            output_video_stream = next((s for s in probe['streams'] if s['codec_type'] == 'video'), None)
            output_duration = int(float(probe['format']['duration'])) if 'duration' in probe['format'] else 0
            output_width = output_video_stream['width'] if output_video_stream else 0
            output_height = output_video_stream['height'] if output_video_stream else 0
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
