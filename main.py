import tkinter as tk
import pywinstyles
from PIL import ImageTk, Image
from config import STYLE_CONFIG


def center_window(window, width, height):
    screen_width = window.winfo_screenwidth()
    screen_height = window.winfo_screenheight()
    x = (screen_width - width) // 2
    y = (screen_height - height) // 2
    window.geometry(f"{width}x{height}+{x}+{y}")


bg_original = Image.open("ctj.jpg").resize((1200, 800))
# 主窗口布局
def load_main_tk(main):
    root = tk.Tk()
    root.title("Bilibili")

    # 窗口大小+居中
    root.geometry("1200x800")
    center_window(root, 1200, 800)
    root.resizable(False, False)

    # 加载原始背景图
    bg_photo = ImageTk.PhotoImage(bg_original)

    # 插入根窗口背景图片
    bg_label = tk.Label(root, image=bg_photo)
    bg_label.image = bg_photo  # 保留引用防止被GC回收
    bg_label.place(x=0, y=0, relwidth=1, relheight=1)

    top_frame = tk.Frame(root,bg="white",height=80)
    top_frame.pack(fill=tk.X, padx=20, pady=20)
    top_frame.pack_propagate(False)
    pywinstyles.set_opacity(top_frame, value=0.9)



    # 用户信息标签（修复：移除transparent，改用与背景图融合的方式）
    userinfo = tk.Label(
        top_frame,
        text="未登录",
        font=STYLE_CONFIG["title_font"],
        fg=STYLE_CONFIG['accent_color'],
        bg="white",
        name="userinfo"
    )
    userinfo.pack(side=tk.LEFT, padx=20, pady=20)

    btn_frame = tk.Frame(root, bg="white", height=100)
    btn_frame.pack(fill=tk.X, padx=20, pady=10)
    btn_frame.pack_propagate(False)
    pywinstyles.set_opacity(btn_frame, value=0.9)

    # 登录按钮（放在背景Label之后）
    btn_login = tk.Button(btn_frame, text="登录", command=main.login,font=STYLE_CONFIG["normal_font"],
        bg="#00a1d6",
        fg="white",
        bd=0,
        relief=tk.FLAT,
        padx=20,
        pady=8,
        cursor="hand2")
    btn_login.grid(row=0, column=0, padx=20, pady=30)

    # 搜索按钮
    btn_search = tk.Button(btn_frame, text="搜索", command=main.search,font=STYLE_CONFIG["normal_font"],
        bg="#00a1d6",
        fg="white",
        bd=0,
        relief=tk.FLAT,
        padx=20,
        pady=8,
        cursor="hand2")
    btn_search.grid(row=0, column=2, padx=20, pady=30)

    # 个人信息按钮
    btn_myself = tk.Button(btn_frame, text="我的", command=main.check_myself,font=STYLE_CONFIG["normal_font"],
        bg="#00a1d6",
        fg="white",
        bd=0,
        relief=tk.FLAT,
        padx=20,
        pady=8,
        cursor="hand2")
    btn_myself.grid(row=0, column=1, padx=20, pady=30)

    # 消息按钮
    btn_message = tk.Button(btn_frame, text="下载列表", command=main.show_download_list,font=STYLE_CONFIG["normal_font"],
        bg="#00a1d6",
        fg="white",
        bd=0,
        relief=tk.FLAT,
        padx=20,
        pady=8,
        cursor="hand2")
    btn_message.grid(row=0, column=3, padx=20, pady=30)

    # 按钮居中分布
    for col in range(4):
        btn_frame.grid_columnconfigure(col, weight=1)

    content_frame = tk.Frame(root, bg="white")
    content_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)
    pywinstyles.set_opacity(content_frame, value=0.9)

    return root, userinfo, content_frame