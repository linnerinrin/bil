import tkinter as tk
from io import BytesIO
import requests
from PIL import ImageTk
from PIL import Image

from config import STYLE_CONFIG

def load_avatar(face_url):
    avatar_photo = None
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        response = requests.get(face_url, headers=headers, timeout=5)
        response.raise_for_status()

        img_data = BytesIO(response.content)
        avatar_img = Image.open(img_data).resize((80, 80))
        avatar_photo = ImageTk.PhotoImage(avatar_img)
    except Exception as e:
        print(f"头像加载失败: {e}")
    return avatar_photo

# 加载个人信息界面
def load_check_ui(target_frame, user_info):
    if user_info is None:
        tip_label = tk.Label(
            target_frame,
            text="未登录",
            font=STYLE_CONFIG["title_font"],
            bg=STYLE_CONFIG["frame_bg"],
            fg=STYLE_CONFIG["text_color"],
            justify=tk.CENTER
        )
        tip_label.pack(expand=True)
        return
    else:
        uid = user_info["mid"]
        nickname = user_info["name"] or "未设置昵称"
        sex = user_info["sex"] or "未知"
        sign = user_info["sign"] or "暂无个性签名"
        coins = user_info.get("coins", 0)
        following = user_info["following"]
        follower = user_info.get("follower", user_info.get("fans", 0))
        face_url = user_info["face"]
        level = user_info["level"]

        # 加载头像
        avatar_photo = load_avatar(face_url)

        # 头像容器（左）
        avatar_frame = tk.Frame(target_frame, bg=STYLE_CONFIG["frame_bg"])
        avatar_frame.pack(side=tk.LEFT, padx=20, pady=15)

        # 头像标签（加载失败显示文字）
        avatar_label = tk.Label(
            avatar_frame,
            image=avatar_photo,
            compound=tk.TOP,
            font=("微软雅黑", 10),
            bg=STYLE_CONFIG["frame_bg"],
            relief=tk.RAISED,
            bd=1,
            width=80,
            height=80
        )
        avatar_label.image = avatar_photo
        avatar_label.pack()

        # 信息容器（右）
        info_frame = tk.Frame(target_frame, bg=STYLE_CONFIG["frame_bg"])
        info_frame.pack(side=tk.LEFT, padx=10, pady=15, fill=tk.X, expand=True)

        # 信息文本排版
        line1 = tk.Label(
            info_frame,
            text=f"🆔 {nickname} (UID: {uid}) | 📊 等级: {level}",
            font=("微软雅黑", 12, "bold"),
            bg=STYLE_CONFIG["frame_bg"],
            fg="#2E86AB"
        )
        line1.pack(anchor=tk.W, pady=(0, 5))

        line2 = tk.Label(
            info_frame,
            text=f"👤 性别: {sex}",
            font=("微软雅黑", 10),
            bg=STYLE_CONFIG["frame_bg"],
            fg="#555555"
        )
        line2.pack(anchor=tk.W, pady=2)

        line3 = tk.Label(
            info_frame,
            text=f"💰 硬币: {coins} | 🧑‍🤝‍🧑 关注: {following} | 👥 粉丝: {follower}",
            font=("微软雅黑", 10),
            bg=STYLE_CONFIG["frame_bg"],
            fg="#555555"
        )
        line3.pack(anchor=tk.W, pady=2)

        line4 = tk.Label(
            info_frame,
            text=f"✍️ 个性签名: {sign}",
            font=("微软雅黑", 10),
            bg=STYLE_CONFIG["frame_bg"],
            fg="#555555",
            wraplength=500,
            justify=tk.LEFT
        )
        line4.pack(anchor=tk.W, pady=(2, 0))

        # 美化frame
        target_frame.config(relief=tk.GROOVE)