import asyncio
import os
import re
import threading
import pyperclip
import requests
from bilibili_api import login_v2, user,search,video
from PIL import Image
from list import show_download_list, refresh_download_list
from main import load_main_tk
from login import load_login_ui
from search import load_search_ui,show_search_results,show_tip
from profile import load_check_ui
from merge import merge_video_audio
from tkinter import messagebox

class Bilibili:
    def __init__(self):
        self.credential = None
        self.user_info = None
        self.main_window, self.top_frame, self.content_frame = load_main_tk(self)
        self.qr_login_instance = None
        self.download_dir = "downloads"
        os.makedirs(self.download_dir, exist_ok=True)
        self.download_tasks = []
        self.download_tree = None

    # 登录入口（触发登录界面渲染）
    def login(self):
        qr_code_image = self._generate_login_qrcode()
        self._clear_content_frame()
        load_login_ui(
            content_frame=self.content_frame,
            login_qrcode=qr_code_image,
            login_success_callback=self._on_login_success
        )
        self._start_qrcode_login_polling()

    # 生成登录二维码
    def _generate_login_qrcode(self):
        async def async_generate():
            self.qr_login_instance = login_v2.QrCodeLogin()
            await self.qr_login_instance.generate_qrcode()
            qr_pic = self.qr_login_instance.get_qrcode_picture()
            qr_pic.to_file("login_qrcode.png")
            return Image.open("login_qrcode.png").resize((300, 300))
        return asyncio.run(async_generate())

    # 二维码登录状态轮询
    def _start_qrcode_login_polling(self):
        async def async_check_status():
            if not self.qr_login_instance:
                return
            try:
                status = await self.qr_login_instance.check_state()
                if status == login_v2.QrCodeLoginEvents.DONE:
                    credential = self.qr_login_instance.get_credential()
                    self._on_login_success(credential)
                    self._clear_content_frame()
                else:
                    self.main_window.after(1000, lambda: asyncio.run(async_check_status()))
            except Exception as e:
                print(f"二维码登录状态检查异常: {e}")
                self.main_window.after(1000, lambda: asyncio.run(async_check_status()))
        self.main_window.after(1000, lambda: asyncio.run(async_check_status()))

    # 登录成功回调
    def _on_login_success(self, credential: login_v2.Credential):
        self.credential = credential
        # 更新顶部用户信息
        self.main_window.after(100, lambda: asyncio.run(self._fetch_user_info()))
        # 延迟获取用户信息（避免UI阻塞）
        self.qr_login_instance = None

    # 获取用户信息
    async def _fetch_user_info(self):

        self.user_info = await user.get_self_info(credential=self.credential)
        self.top_frame.config(
            text=f"欢迎，{self.user_info['name'] or '未设置昵称'}",
            fg="#00a1d6")
        self.check_myself()

    def search(self):
        """入口：加载搜索UI"""
        self._clear_content_frame()
        load_search_ui(
            content_frame=self.content_frame,
            search_callback=self._do_search,
            copy_callback=self._copy_video_info,
            download_callback=self._download_video,
            audio_callback=self._download_audio
        )


    def show_download_list(self):
        self._clear_content_frame()
        """显示下载列表UI（绑定到外部按钮）"""
        self.download_tree = show_download_list(self.content_frame, self.download_tasks,self.download_dir)
        self._refresh_download_list(self.download_tree)
    def _refresh_download_list(self, tree=None):
        """刷新下载进度"""
        if tree is None:
            tree = self.download_tree
        if tree:
            refresh_download_list(tree, self.download_tasks)

    async def _do_search(self, search_content: str):
        """核心搜索逻辑：区分BV号/关键词"""
        search_results = []
        bv_pattern = re.compile(r'^BV[a-zA-Z0-9]{10,}$')

        try:
            if bv_pattern.match(search_content.strip()):
                # BV号搜索
                v = video.Video(bvid=search_content.strip(), credential=self.credential)
                video_info = await v.get_info()
                search_results.append({
                    "bvid": video_info["bvid"],
                    "cid": video_info["cid"],
                    "title": re.sub(r'<.*?>', '', video_info["title"]).replace("/", "_").replace("\\", "_").replace(":",
                                                                                                                    "_"),
                    "author": video_info["owner"]["name"],
                    "play": video_info["stat"]["view"],
                    "danmaku": video_info["stat"]["danmaku"]
                })
            else:
                # 关键词搜索
                if not search_content.strip():
                    show_tip(self.content_frame, "请输入搜索关键词")
                    return
                search_res = await search.search_by_type(
                    keyword=search_content.strip(),
                    search_type=search.SearchObjectType.VIDEO,
                    page_size=20
                )
                for item in search_res["result"]:
                    v_temp = video.Video(bvid=item["bvid"], credential=self.credential)
                    v_info = await v_temp.get_info()
                    search_results.append({
                        "bvid": item["bvid"],
                        "cid": v_info["cid"],
                        "title": re.sub(r'<.*?>', '', item["title"]).replace("/", "_").replace("\\", "_").replace(":",
                                                                                                                  "_"),
                        "author": item["author"],
                        "play": item["play"],
                        "danmaku": item["danmaku"]
                    })
        except Exception as e:
            show_tip(self.content_frame, f"搜索失败：{str(e)}")
            return

        show_search_results(
            content_frame=self.content_frame,
            results=search_results,
            copy_callback=self._copy_video_info,
            download_callback=self._download_video,
            audio_callback=self._download_audio
        )

    def _copy_video_info(self, video_data: dict):
        """复制视频信息到剪贴板"""
        copy_text = f"""
    BV号：{video_data['bvid']}
    标题：{video_data['title']}
    UP主：{video_data['author']}
    播放量：{video_data['play']}
    弹幕数：{video_data['danmaku']}
            """.strip()
        pyperclip.copy(copy_text)


    def _download_video(self, video_data: dict):

        confirm = messagebox.askyesno("确认下载", f"是否下载：\n{video_data['title']}")
        if not confirm:
            return

        # 添加下载任务到列表
        task = {
            "bvid": video_data["bvid"],
            "title": video_data["title"],
            "progress": 0,
            "status": "下载中",
            "save_path": ""
        }
        self.download_tasks.append(task)

        # 异步执行下载
        t=threading.Thread(target=self.safe_download,args=(video_data,task))
        t.start()

    def _download_audio(self, video_data: dict):

        confirm = messagebox.askyesno("确认下载", f"是否下载：\n{video_data['title']}")
        if not confirm:
            return

        # 添加下载任务到列表
        task = {
            "bvid": video_data["bvid"],
            "title": video_data["title"],
            "progress": 0,
            "status": "下载中",
            "save_path": ""
        }
        self.download_tasks.append(task)

        # 异步执行下载
        t = threading.Thread(target=self.safe_download_audio, args=(video_data, task))
        t.start()

    def safe_download(self,video_data,task):
        asyncio.run(self._do_download(video_data,task))

    def safe_download_audio(self,video_data,task):
        asyncio.run(self.audio_do_download(video_data,task))

    async def _do_download(self, video_data: dict, task: dict):
        try:
            # 1. 获取视频下载地址
            v = video.Video(bvid=video_data["bvid"], credential=self.credential)
            download_url = await v.get_download_url(cid=video_data["cid"])
            dash = download_url["dash"]

            # 2. 获取最高质量音视频地址
            video_url = max(dash["video"], key=lambda x: x["bandwidth"])["baseUrl"]
            audio_url = max(dash["audio"], key=lambda x: x["bandwidth"])["baseUrl"]

            # 3. 临时文件路径
            video_tmp = os.path.join(self.download_dir, f"{video_data['bvid']}_video.tmp")
            audio_tmp = os.path.join(self.download_dir, f"{video_data['bvid']}_audio.tmp")
            output_mp4 = os.path.join(self.download_dir, f"{video_data['title']}{video_data['bvid']}.mp4")
            task["save_path"] = output_mp4

            # 下载请求头
            headers = {
                "Referer": f"https://www.bilibili.com/video/{video_data['bvid']}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }

            self._download_file(video_url, video_tmp, headers, "视频", task)
            self._download_file(audio_url, audio_tmp, headers, "音频", task, offset=50)
            task["status"] = "合并中"
            task["progress"] = 100
            merge_video_audio(video_tmp,audio_tmp,output_mp4)
            task["progress"] = 100
            task["status"] = "完成"
        except Exception as e:
            task["status"] = "失败"
            task["progress"] = -1

    def _download_file(self, url: str, save_path: str, headers: dict, file_type: str, task: dict,
                             offset: int = 0):
        session=requests.Session()
        with session.get(url, headers=headers,stream=True) as resp:
            resp.raise_for_status()
            total_size = int(resp.headers.get("Content-Length", 0))
            downloaded_size = 0

            with open(save_path, "wb") as f:
                for chunk in resp.iter_content(1024 * 1024):
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        # 更新进度（视频0-50%，音频50-90%）
                        if total_size > 0:
                            progress = int((downloaded_size / total_size) * 50) + offset
                            task["progress"] = progress


    async def audio_do_download(self, video_data: dict, task: dict):
        try:
            v = video.Video(bvid=video_data["bvid"], credential=self.credential)
            download_url = await v.get_download_url(cid=video_data["cid"])
            dash = download_url["dash"]
            audio_url = max(dash["audio"], key=lambda x: x["bandwidth"])["baseUrl"]
            audio_tmp = os.path.join(self.download_dir, f"{video_data['bvid']}_audio.mp3")
            task["save_path"] = audio_tmp
            # 下载请求头
            headers = {
                "Referer": f"https://www.bilibili.com/video/{video_data['bvid']}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            self.audio_download_file(audio_url, audio_tmp, headers, "音频", task)
            task["progress"] = 100
            task["status"] = "完成"
        except Exception as e:
            task["status"] = "失败"
            task["progress"] = -1

    def audio_download_file(self, url: str, save_path: str, headers: dict, file_type: str, task: dict,
                             offset: int = 0):
        session=requests.Session()
        with session.get(url, headers=headers,stream=True) as resp:
            resp.raise_for_status()
            total_size = int(resp.headers.get("Content-Length", 0))
            downloaded_size = 0

            with open(save_path, "wb") as f:
                for chunk in resp.iter_content(1024 * 1024):
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        if total_size > 0:
                            progress = int((downloaded_size / total_size) * 100)
                            task["progress"] = progress


    # 个人信息展示
    def check_myself(self):
        self._clear_content_frame()
        load_check_ui(self.content_frame, self.user_info)

    # 清空内容容器
    def _clear_content_frame(self):
        for widget in self.content_frame.winfo_children():
            widget.destroy()


    # 启动应用
    def run(self):
        self.main_window.mainloop()