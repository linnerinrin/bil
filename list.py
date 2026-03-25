
import tkinter as tk
from tkinter import ttk, messagebox
import os
from config import STYLE_CONFIG

def show_download_list(content_frame, download_tasks,download_dir):
    # 创建下载列表标题
    title_label = tk.Label(
        content_frame,
        text="下载任务列表",
        font=STYLE_CONFIG["title_font"],
        bg=STYLE_CONFIG["frame_bg"],
        fg=STYLE_CONFIG["text_color"]
    )

    title_label.pack(pady=10)

    # 创建表格展示下载任务
    tree = ttk.Treeview(
        content_frame,
        columns=("bvid", "title", "progress", "status"),
        show="headings",
        height=10
    )
    # 设置列标题
    tree.heading("bvid", text="BV号")
    tree.heading("title", text="视频标题")
    tree.heading("progress", text="下载进度")
    tree.heading("status", text="状态")
    # 设置列宽
    tree.column("bvid", width=120)
    tree.column("title", width=400)
    tree.column("progress", width=100)
    tree.column("status", width=100)
    tree.pack(fill="both", expand=True, padx=20, pady=10)


    # 填充下载任务
    for task in download_tasks:
        progress_text = f"{task['progress']}%" if task['progress'] >= 0 else "未知"
        tree.insert("", "end", values=(
            task["bvid"],
            task["title"],
            progress_text,
            task["status"],
        ), tags=(task["bvid"],))

    def open_folder():
        try:
            os.makedirs(download_dir, exist_ok=True)
            os.startfile(download_dir)
        except:
            messagebox.showwarning("提示", "文件夹不存在")

    btn=tk.Button(content_frame,text="打开下载目录",command=open_folder)
    btn.pack()

    return tree


def refresh_download_list(tree, download_tasks):
    # 清空表格
    for item in tree.get_children():
        tree.delete(item)
    # 重新填充
    for task in download_tasks:
        progress_text = f"{task['progress']}%" if task['progress'] >= 0 else "未知"
        tree.insert("", "end", values=(
            task["bvid"],
            task["title"],
            progress_text,
            task["status"]
        ), tags=(task["bvid"],))
    tree.after(50, lambda: refresh_download_list(tree,download_tasks))
