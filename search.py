from config import STYLE_CONFIG
import tkinter as tk
import asyncio


def load_search_ui(content_frame, search_callback, copy_callback,download_callback,audio_callback):
    search_frame = tk.Frame(content_frame, bg="white")
    search_frame.pack(pady=20, fill="x", padx=50)

    # 搜索输入框
    search_var = tk.StringVar()
    search_entry = tk.Entry(
        search_frame,
        textvariable=search_var,
        font=STYLE_CONFIG["normal_font"],
        bg="white",
        fg=STYLE_CONFIG["text_color"],
        bd=2,
        relief=tk.FLAT,
        insertbackground=STYLE_CONFIG["text_color"],
        highlightthickness=1,
        highlightcolor="#00a1d6"
    )
    search_entry.pack(side="left", fill="x", expand=True, ipady=8, padx=(0, 10))
    search_entry.focus()

    def on_search_click():
        search_content = search_var.get().strip()
        if not search_content:
            show_tip(content_frame, "请输入BV号或搜索关键词")
            return
        # 异步执行搜索
        asyncio.run(search_callback(search_content))

    # 搜索按钮
    search_btn = tk.Button(
        search_frame,
        text="搜索",
        command=on_search_click,
        font=STYLE_CONFIG["normal_font"],
        bg="#00a1d6",
        fg="white",
        bd=0,
        relief=tk.FLAT,
        padx=20,
        pady=8,
        cursor="hand2"
    )
    search_btn.pack(side="right")

    # 回车触发搜索
    search_entry.bind("<Return>", lambda e: on_search_click())
    canvas = tk.Canvas(content_frame, highlightthickness=0)

    scrollbar = tk.Scrollbar(content_frame, orient="vertical", command=canvas.yview, bg=STYLE_CONFIG["frame_bg"])
    scrollable_frame = tk.Frame(canvas, bg=STYLE_CONFIG["frame_bg"])

    # 绑定滚动区域
    scrollable_frame.bind(
        "<Configure>",
        lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
    )

    def on_mouse_wheel(event):
        # Windows/Linux/Mac 滚轮事件兼容
        delta = event.delta if hasattr(event, 'delta') else -event.delta
        canvas.yview_scroll(int(-delta / 120), "units")

    # 绑定滚轮事件到画布和滚动容器
    canvas.bind("<MouseWheel>", on_mouse_wheel)
    scrollable_frame.bind("<MouseWheel>", on_mouse_wheel)
    content_frame.bind("<MouseWheel>", on_mouse_wheel)  # 全局绑定

    canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)

    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    content_frame.search_canvas = canvas
    content_frame.scrollable_frame = scrollable_frame
    content_frame.scrollbar = scrollbar


def show_tip(content_frame, tip_text: str):
    # 清空滚动容器内的内容（而非销毁容器）
    for widget in content_frame.scrollable_frame.winfo_children():
        widget.destroy()

    tip_label = tk.Label(
        content_frame.scrollable_frame,
        text=tip_text,
        font=STYLE_CONFIG["title_font"],
        bg=STYLE_CONFIG["frame_bg"],
        fg=STYLE_CONFIG["text_color"],
        justify=tk.CENTER
    )
    tip_label.pack(expand=True)


def show_search_results(content_frame, results: list, copy_callback, download_callback,audio_callback):
    for widget in content_frame.scrollable_frame.winfo_children():
        widget.destroy()

    if not results:
        show_tip(content_frame, "未找到相关视频")
        return

    for idx, video in enumerate(results):
        # 单个视频项容器
        item_frame = tk.Frame(content_frame.scrollable_frame, bg=STYLE_CONFIG["frame_bg"], bd=1, relief=tk.GROOVE)
        item_frame.pack(fill="x", padx=10, pady=5)

        # 视频信息区域
        info_frame = tk.Frame(item_frame, bg=STYLE_CONFIG["frame_bg"])
        info_frame.pack(side="left", fill="x", expand=True, padx=10, pady=5)

        # BV号标签
        bvid_label = tk.Label(
            info_frame, text=f"BV号: {video['bvid']}",
            font=STYLE_CONFIG["normal_font"], bg=STYLE_CONFIG["frame_bg"], fg="#00a1d6"
        )
        bvid_label.pack(anchor="w")

        # 标题标签（
        title_label = tk.Label(
            info_frame, text=f"标题: {video['title']}",
            font=STYLE_CONFIG["title_font"], bg=STYLE_CONFIG["frame_bg"], fg=STYLE_CONFIG["text_color"],
            wraplength=700, justify=tk.LEFT
        )
        title_label.pack(anchor="w", pady=2)

        # 附加信息标签
        info_label = tk.Label(
            info_frame, text=f"UP主: {video['author']} | 播放: {video['play']} | 弹幕: {video['danmaku']}",
            font=STYLE_CONFIG["small_font"], bg=STYLE_CONFIG["frame_bg"], fg=STYLE_CONFIG["text_color"]
        )
        info_label.pack(anchor="w")

        # 操作按钮区域
        btn_frame = tk.Frame(item_frame, bg=STYLE_CONFIG["frame_bg"])
        btn_frame.pack(side="right", padx=10, pady=5)

        # 复制按钮
        copy_btn = tk.Button(
            btn_frame,
            text="复制信息",
            command=lambda v=video: copy_callback(v),
            font=STYLE_CONFIG["small_font"],
            bg="#4CAF50",
            fg="white",
            bd=0,
            relief=tk.FLAT,
            padx=10,
            pady=5,
            cursor="hand2"
        )
        copy_btn.pack(fill="x", pady=2)

        # 下载按钮
        download_btn = tk.Button(
            btn_frame,
            text="下载视频",
            command=lambda v=video: download_callback(v),
            font=STYLE_CONFIG["small_font"],
            bg="#ff9800",
            fg="white",
            bd=0,
            relief=tk.FLAT,
            padx=10,
            pady=5,
            cursor="hand2"
        )
        download_btn.pack(fill="x", pady=2)

        audio_btn = tk.Button(
            btn_frame,
            text="下载音频",
            command=lambda v=video: audio_callback(v),
            font=STYLE_CONFIG["small_font"],
            bg="#aa9800",
            fg="white",
            bd=0,
            relief=tk.FLAT,
            padx=10,
            pady=5,
            cursor="hand2"
        )
        audio_btn.pack(fill="x",pady=2)

    # 更新滚动区域
    content_frame.search_canvas.configure(scrollregion=content_frame.search_canvas.bbox("all"))