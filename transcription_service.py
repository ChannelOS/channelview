"""
ChannelView Transcription Service
Provides server-side speech-to-text for video responses.
Uses OpenAI Whisper (local) when available, or returns empty transcript.
Browser-side Web Speech API handles real-time transcription in the interview UI.
"""
import os
import tempfile
import subprocess

# Check for Whisper availability
WHISPER_AVAILABLE = False
try:
    import whisper
    WHISPER_AVAILABLE = True
    _model = None
except ImportError:
    pass


def get_whisper_model():
    """Lazy-load Whisper model (base is fast + good enough for interviews)."""
    global _model
    if _model is None:
        _model = whisper.load_model("base")
    return _model


def is_transcription_available():
    """Check if server-side transcription is available."""
    return WHISPER_AVAILABLE


def transcribe_video(video_path):
    """
    Transcribe a video file to text.
    Returns: { 'transcript': str, 'language': str, 'duration': float }
    """
    if not os.path.exists(video_path):
        return {'transcript': '', 'error': 'File not found'}

    if not WHISPER_AVAILABLE:
        return {'transcript': '', 'error': 'Whisper not installed. Install with: pip install openai-whisper'}

    try:
        # Extract audio from video (Whisper needs audio format)
        audio_path = None
        if video_path.endswith('.webm') or video_path.endswith('.mp4'):
            audio_path = tempfile.mktemp(suffix='.wav')
            result = subprocess.run(
                ['ffmpeg', '-i', video_path, '-vn', '-acodec', 'pcm_s16le',
                 '-ar', '16000', '-ac', '1', audio_path, '-y'],
                capture_output=True, timeout=60
            )
            if result.returncode != 0:
                # Try feeding video directly to Whisper
                audio_path = video_path
        else:
            audio_path = video_path

        model = get_whisper_model()
        result = model.transcribe(audio_path, language='en')

        # Clean up temp audio
        if audio_path != video_path and os.path.exists(audio_path):
            os.remove(audio_path)

        return {
            'transcript': result.get('text', '').strip(),
            'language': result.get('language', 'en'),
            'duration': result.get('duration', 0)
        }
    except Exception as e:
        return {'transcript': '', 'error': str(e)}


def transcribe_all_responses(db, candidate_id, video_base_path='static/uploads/videos'):
    """
    Transcribe all video responses for a candidate that don't have transcripts yet.
    Returns count of newly transcribed responses.
    """
    responses = db.execute(
        'SELECT id, video_path FROM responses WHERE candidate_id=? AND (transcript IS NULL OR transcript="")',
        (candidate_id,)
    ).fetchall()

    count = 0
    for resp in responses:
        if not resp['video_path']:
            continue

        video_file = resp['video_path']
        if not video_file.startswith('/'):
            # Relative path â resolve from app root
            video_file = os.path.join(os.path.dirname(__file__), video_file)

        result = transcribe_video(video_file)
        if result['transcript']:
            db.execute('UPDATE responses SET transcript=? WHERE id=?',
                       (result['transcript'], resp['id']))
            count += 1

    if count:
        db.commit()
    return count
