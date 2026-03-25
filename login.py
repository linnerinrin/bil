import tkinter as tk
from tkinter import messagebox
import asyncio
from PIL import ImageTk
from bilibili_api import login_v2
from bilibili_api.utils.geetest import Geetest, GeetestType
from config import STYLE_CONFIG


# 嵌入式登录界面
def load_login_ui(content_frame, login_qrcode, login_success_callback):
    # 二维码区域
    qrcode_frame = tk.Frame(content_frame, width=400)
    qrcode_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    # 二维码标题
    qrcode_title = tk.Label(
        qrcode_frame,
        text="扫码登录",
        font=STYLE_CONFIG["title_font"],
        bg=STYLE_CONFIG["frame_bg"],
        fg=STYLE_CONFIG["text_color"]
    )
    qrcode_title.pack(pady=(30, 10))

    # 二维码图片
    login_qrcode_tk = ImageTk.PhotoImage(image=login_qrcode)
    codeimg = tk.Label(
        qrcode_frame,
        image=login_qrcode_tk,
        bg=STYLE_CONFIG["frame_bg"],
        anchor="center"
    )
    codeimg.image = login_qrcode_tk
    codeimg.pack(pady=(0, 20), anchor="center")

    # 二维码提示文字
    qrcode_tip = tk.Label(
        qrcode_frame,
        text="打开bilibili客户端 在「我的」页面左上角扫描二维码",
        font=("Microsoft YaHei", 11),
        bg=STYLE_CONFIG["frame_bg"],
        fg="#333333",
        wraplength=300,
        justify="center",
        anchor="center"
    )
    qrcode_tip.pack(
        pady=(0, 20),
        anchor="center",
        fill=tk.X,
        padx=20
    )

    # 账密登录区域
    pwd_frame = tk.Frame(content_frame, width=400)
    pwd_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    # 账密登录标题
    pwd_title = tk.Label(
        pwd_frame,
        text="账号密码登录",
        font=STYLE_CONFIG["title_font"],
        bg=STYLE_CONFIG["frame_bg"],
        fg=STYLE_CONFIG["text_color"]
    )
    pwd_title.pack(pady=50)

    # 手机号/邮箱输入框
    user_frame = tk.Frame(pwd_frame, bg=STYLE_CONFIG["frame_bg"])
    user_frame.pack(pady=15, fill=tk.X, padx=50)
    user_label = tk.Label(
        user_frame,
        text="邮箱/手机号：",
        font=STYLE_CONFIG["font"],
        bg=STYLE_CONFIG["frame_bg"],
        fg=STYLE_CONFIG["text_color"],
        width=10
    )
    user_label.pack(side=tk.LEFT)
    usenumber_entry = tk.Entry(
        user_frame,
        font=STYLE_CONFIG["font"],
        relief=tk.FLAT,
        borderwidth=1,
        highlightthickness=1,
        highlightcolor=STYLE_CONFIG["accent_color"],
        highlightbackground="#cccccc"
    )
    usenumber_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=10)

    # 密码输入框
    pwd_entry_frame = tk.Frame(pwd_frame, bg=STYLE_CONFIG["frame_bg"])
    pwd_entry_frame.pack(pady=15, fill=tk.X, padx=50)
    pwd_label = tk.Label(
        pwd_entry_frame,
        text="账户密码：",
        font=STYLE_CONFIG["font"],
        bg=STYLE_CONFIG["frame_bg"],
        fg=STYLE_CONFIG["text_color"],
        width=10
    )
    pwd_label.pack(side=tk.LEFT)
    password_entry = tk.Entry(
        pwd_entry_frame,
        font=STYLE_CONFIG["font"],
        show="●",
        relief=tk.FLAT,
        borderwidth=1,
        highlightthickness=1,
        highlightcolor=STYLE_CONFIG["accent_color"],
        highlightbackground="#cccccc"
    )
    password_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=10)

    # 错误提示标签（初始隐藏）
    error_label = tk.Label(
        pwd_frame,
        text="",
        font=STYLE_CONFIG["font"],
        fg="red",
        bg=STYLE_CONFIG["frame_bg"]
    )


    # 账密登录逻辑
    """def check_password_login():
        username = usenumber_entry.get()
        password = password_entry.get()
        if not username or not password:
            error_label.config(text="账号/密码不能为空！")
            error_label.after(3000, lambda: error_label.config(text=""))
            return

        async def run_login():
            geek = Geetest()
            # 先创建验证服务
            await geek.generate_test(GeetestType.LOGIN)
            geek.start_geetest_server()

            # 弹出验证地址
            url = geek.get_geetest_server_url()
            error_label.config(text=f"请打开验证：{url}")
            print(f"👉 打开浏览器完成验证：{url}")

            # 等待验证完成
            while not geek.has_done():
                await asyncio.sleep(1)

            # 登录
            credential = await login_v2.login_with_password(
                username=username,
                password=password,
                geetest=geek
            )
            geek.close_geetest_server()
            return credential"""

    """
        # 执行登录
        try:
            credential = asyncio.run(run_login())
            login_success_callback(credential)
            error_label.config(text="✅ 登录成功！")
            error_label.after(2000, lambda: error_label.config(text=""))
        except Exception as e:
            error_label.config(text=f"登录失败: {str(e)}")
            error_label.after(5000, lambda: error_label.config(text=""))
            print(f"登录失败详情: {e}")"""

    # 登录按钮
    submit_btn = tk.Button(
        pwd_frame,
        text="登录",
        command=lambda :messagebox.showinfo("错误","未实现"),
        width=15
    )
    submit_btn.pack()

    return usenumber_entry, password_entry, error_label