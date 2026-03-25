import os
import threading
from moviepy.audio.io.AudioFileClip import AudioFileClip
from moviepy.video.io.VideoFileClip import VideoFileClip

def merge_video_audio(video_path, audio_path, output_path):
    video = VideoFileClip(video_path)
    audio = AudioFileClip(audio_path)
    final_video = video.with_audio(audio)

    # 保存
    final_video.write_videofile(
        output_path,
        codec="libx264",
        audio_codec="aac"
    )
    try:
        video.close()
    except:
        pass
    try:
        audio.close()
    except:
        pass
    try:
        final_video.close()
    except:
        pass
    os.remove(video_path)
    os.remove(audio_path)

