    # -*- coding: utf-8 -*-
"""
学校官网关键词监控助手 - 完整版
功能：
1. 定时抓取指定栏目的文章，筛选含有关键词的内容
2. 自动生成中文摘要
3. 记录已处理文章，避免重复通知
4. 支持 Windows 桌面弹窗和微信（Server酱）推送
5. 图形界面（tkinter）：管理关键词、查看历史文章、手动检查、过滤搜索
"""

from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox
import threading
import time
import json
import os
import hashlib
import re
from urllib.parse import urljoin
from datetime import datetime
from PIL import Image, ImageTk
import requests
from bs4 import BeautifulSoup
from sumy.parsers.plaintext import PlaintextParser
from sumy.nlp.tokenizers import Tokenizer
from sumy.summarizers.text_rank import TextRankSummarizer
import json
import os
import sys

# 配置文件名
CONFIG_FILE = "config.json"
#基本配置
def load_config():
    default_config = {
        "TARGET_URLS": [
           "https://jww.zjgsu.edu.cn/main.htm"  # 学校教务处官网
        ],
        "KEYWORDS": ["奖学金", "竞赛", "实习", "考试", "转专业", "计算机", "放假"],
        "SERVERCHAN_SENDKEY": "SCT348388TBGLiTMs86CFVtA55lq6muRtB",
        "CHECK_INTERVAL_MINUTES": 30,
        "MAX_AGE_DAYS": 7,
        "ENABLE_DESKTOP_NOTIFY": True
    }

    config_path = None
    if getattr(sys, 'frozen', False):
        # 打包后 .exe 运行环境
        config_path = os.path.join(os.path.dirname(sys.executable), CONFIG_FILE)
    else:
        # 开发环境直接运行 .py
        config_path = CONFIG_FILE

    if os.path.exists(config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                user_config = json.load(f)
                default_config.update(user_config)  # 用外部配置覆盖默认值
                print(f"✅ 配置加载成功: {config_path}")
        except Exception as e:
            print(f"⚠️ 读取外部配置失败({e})，使用默认配置")
    else:
        # 如果 config.json 不存在，则用默认配置创建一个模板文件
        try:
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(default_config, f, ensure_ascii=False, indent=2)
            print(f"📝 已创建默认配置文件模板: {config_path}")
        except Exception as e:
            print(f"⚠️ 创建默认配置文件失败: {e}")

    return default_config

config = load_config()  # 将配置作为全局变量供其他模块使用

TARGET_URLS = config["TARGET_URLS"]
KEYWORDS = config["KEYWORDS"]
MAX_AGE_DAYS = config["MAX_AGE_DAYS"]   # 发布时间过滤：只抓取最近 N 天内的文章（0 表示不限制）
SERVERCHAN_SENDKEY = config["SERVERCHAN_SENDKEY"]   # 微信推送的 SendKey (Server酱)，不需要则留空
CHECK_INTERVAL_MINUTES = config["CHECK_INTERVAL_MINUTES"]
ENABLE_DESKTOP_NOTIFY = config["ENABLE_DESKTOP_NOTIFY"] # 是否启用桌面通知（Windows）

# ===========================================================

# 全局文件路径
KEYWORDS_FILE = "keywords.json"
ARTICLES_FILE = "articles.json"
PROCESSED_FILE = "processed_links.json"

# 初始化摘要工具
summarizer = TextRankSummarizer()
tokenizer = Tokenizer("chinese")

# ---------- 桌面通知初始化 ----------
desktop_notifier = None
if ENABLE_DESKTOP_NOTIFY:
    try:
        from plyer import notification
        desktop_notifier = notification
    except ImportError:
        try:
            from win10toast import ToastNotifier
            desktop_notifier = ToastNotifier()
        except ImportError:
            print("桌面通知库未安装，将禁用桌面通知")
            ENABLE_DESKTOP_NOTIFY = False

# ================= 数据持久化函数 =================
def load_keywords():
    if os.path.exists(KEYWORDS_FILE):
        with open(KEYWORDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return ["奖学金", "竞赛", "实习"]  # 默认关键词

def save_keywords(keywords):
    with open(KEYWORDS_FILE, "w", encoding="utf-8") as f:
        json.dump(keywords, f, ensure_ascii=False, indent=2)

def load_processed():
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_processed(processed_set):
    with open(PROCESSED_FILE, "w", encoding="utf-8") as f:
        json.dump(list(processed_set), f, ensure_ascii=False, indent=2)

def load_articles():
    if os.path.exists(ARTICLES_FILE):
        with open(ARTICLES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_articles(articles):
    # 只保留最近500条，避免文件过大
    if len(articles) > 500:
        articles = articles[-500:]
    with open(ARTICLES_FILE, "w", encoding="utf-8") as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)

# ================= 网络和内容提取函数 =================
def fetch_html(url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.encoding = r.apparent_encoding
        return r.text
    except Exception as e:
        print(f"请求失败 {url}: {e}")
        return None

def extract_links_from_page(html, base_url):
    """从列表页提取文章链接（根据常见模式过滤）"""
    soup = BeautifulSoup(html, "lxml")
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith('/20') and 'page.htm' in href:
            full_url = urljoin(base_url, href)
            links.add(full_url)
    # 打印提取到的链接（调试用）
    print(f"从 {base_url} 提取到 {len(links)} 个链接:")
    for link in list(links)[:10]:  # 只打印前10个
        print(f"  {link}")
    return list(links)[:30]

def extract_article_content(html):
    """从文章详情页提取正文文本"""
    soup = BeautifulSoup(html, "lxml")
    # 常见正文容器选择器（可自行添加）
    selectors = ["article", ".article", ".content", "#vsb_content", ".main-content", ".news-content", ".detail"]
    text = ""
    for sel in selectors:
        elem = soup.select_one(sel)
        if elem:
            text = elem.get_text(separator="\n", strip=True)
            break
    if not text:
        text = soup.get_text(separator="\n", strip=True)
    # 过滤太短的行（通常不是正文）
    lines = [line.strip() for line in text.splitlines() if len(line.strip()) > 30]
    print(f"提取正文长度: {len(text)} 字符")
    return "\n".join(lines)

def parse_list_page(html, base_url):
    """从列表页直接提取文章信息，无需请求详情页"""
    soup = BeautifulSoup(html, "lxml")
    articles = []
    
    # 查找包含文章链接的 <a> 标签（通常位于列表中）
    for a in soup.find_all('a', href=True):
        href = a['href']
        # 匹配文章链接模式
        if href.startswith('/20') and 'page.htm' in href:
            title = a.get_text(strip=True)
            if not title:
                continue
            full_url = urljoin(base_url, href)
            
            # 尝试从父元素或周围提取日期
            # 方法1: 查找同级的包含日期的元素
            parent = a.find_parent('li') or a.find_parent('div')
            date_text = ''
            if parent:
                date_elem = parent.find('span', class_='date') or parent.find(class_='time')
                if date_elem:
                    date_text = date_elem.get_text(strip=True)
            
            # 方法2: 如果没找到，从文本末尾提取 YYYY-MM-DD 格式
            if not date_text:
                import re
                match = re.search(r'\d{4}-\d{2}-\d{2}', html[max(0, parent.sourceline - 200):parent.sourceline + 200] if parent else html)
                if match:
                    date_text = match.group()
            
            articles.append({
                'title': title,
                'url': full_url,
                'date': date_text
            })
    
    return articles

def extract_publish_date(html, url):
    """从文章页提取发布日期，返回 datetime 对象"""
    import re
    from datetime import datetime
    
    soup = BeautifulSoup(html, "lxml")
    
    # 常见日期格式的正则
    date_patterns = [
        r'(\d{4}-\d{1,2}-\d{1,2})',           # 2026-04-24
        r'(\d{4}年\d{1,2}月\d{1,2}日)',       # 2026年04月24日
        r'(\d{4}/\d{1,2}/\d{1,2})'            # 2026/04/24
    ]
    
    # 优先在 time 标签、.date 类等中查找
    date_elem = soup.find('time') or soup.find(class_='date') or soup.find(class_='time')
    if date_elem:
        text = date_elem.get_text(strip=True)
        for pattern in date_patterns:
            match = re.search(pattern, text)
            if match:
                try:
                    date_str = match.group(1).replace('年', '-').replace('月', '-').replace('/', '-')
                    return datetime.strptime(date_str, "%Y-%m-%d")
                except:
                    pass
    
    # 在整个页面文本中查找
    body_text = soup.get_text()
    for pattern in date_patterns:
        matches = re.findall(pattern, body_text)
        if matches:
            try:
                date_str = matches[0].replace('年', '-').replace('月', '-').replace('/', '-')
                return datetime.strptime(date_str, "%Y-%m-%d")
            except:
                pass
    
    return None

def generate_summary(text, sentence_count=2):
    """使用TextRank生成摘要"""
    if not text or len(text) < 100:
        return "（内容过短，无法生成摘要）"
    parser = PlaintextParser.from_string(text, tokenizer)
    summary_sentences = summarizer(parser.document, sentence_count)
    return " ".join(str(s) for s in summary_sentences)

def keyword_match(text, keywords, title=''):
    """检查标题或内容是否包含任意关键词"""
    combined = f"{title} {text}".lower()
    return any(kw.lower() in combined for kw in keywords)

# ================= 通知函数 =================
def send_desktop_notify(title, message):
    if not ENABLE_DESKTOP_NOTIFY or desktop_notifier is None:
        return
    try:
        if hasattr(desktop_notifier, "notify"):  # plyer
            desktop_notifier.notify(title=title[:64], message=message[:256], timeout=10)
        else:  # win10toast
            desktop_notifier.show_toast(title[:64], message[:256], duration=10)
    except Exception as e:
        print(f"桌面通知失败: {e}")

def send_wechat_notify(title, content, url):
    if not SERVERCHAN_SENDKEY:
        return
    api_url = f"https://sctapi.ftqq.com/{SERVERCHAN_SENDKEY}.send"
    desp = f"**链接**: {url}\n\n**摘要**: {content}"
    try:
        r = requests.post(api_url, data={"title": title, "desp": desp}, timeout=5)
        if r.json().get("code") != 0:
            print(f"微信推送失败: {r.text}")
    except Exception as e:
        print(f"微信推送异常: {e}")

# ================= 核心监控任务 =================
def run_once(gui_callback=None):
    """
    执行一次检查，发现新文章后保存并通知。
    gui_callback: 用于将新文章实时添加到界面列表的回调函数
    返回值：新文章数量
    """
    print(f"[{datetime.now()}] 开始检查更新...")
    processed = load_processed()
    keywords = load_keywords()
    if not keywords:
        return 0

    new_articles = []

    for list_url in TARGET_URLS:
        html = fetch_html(list_url)
        if not html:
            continue
        article_urls = extract_links_from_page(html, list_url)
        for art_url in article_urls:
            url_hash = hashlib.md5(art_url.encode()).hexdigest()
            if url_hash in processed:
                continue

            art_html = fetch_html(art_url)
            if not art_html:
                continue
            content = extract_article_content(art_html)
            # 发布时间过滤
            if MAX_AGE_DAYS > 0:
                pub_date = extract_publish_date(art_html, art_url)
                if pub_date is None:
                    print(f"[跳过] 无法获取发布日期: {art_url}")
                    processed.add(url_hash)
                    continue
                days_ago = (datetime.now() - pub_date).days
                if days_ago > MAX_AGE_DAYS:
                    print(f"[跳过] 发布时间 {pub_date.date()} 超过 {MAX_AGE_DAYS} 天: {art_url}")
                    processed.add(url_hash)
                    continue

            if not keyword_match(content, keywords):
                processed.add(url_hash)
                continue

            # 提取标题
            soup = BeautifulSoup(art_html, "lxml")
            title_tag = soup.find("title")
            title = title_tag.text.strip() if title_tag else "无标题"
            title = re.split(r"[-|_]", title)[0].strip()
            if len(title) > 100:
                title = title[:97] + "..."

            summary = generate_summary(content)

            article_info = {
                "title": title,
                "url": art_url,
                "summary": summary,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            new_articles.append(article_info)
            processed.add(url_hash)

            # 发送通知
            send_desktop_notify(title, summary[:200])
            send_wechat_notify(title, summary, art_url)

    # 保存已处理链接
    save_processed(processed)

    if new_articles:
        # 追加到历史文章文件
        all_articles = load_articles()
        all_articles.extend(new_articles)
        save_articles(all_articles)
        # 如果有界面回调，通知界面更新
        if gui_callback:
            gui_callback(new_articles)

    print(f"[{datetime.now()}] 检查完成，发现 {len(new_articles)} 篇新文章")
    for list_url in TARGET_URLS:
        print(f"列表页: {list_url}")
        html = fetch_html(list_url)
        if not html:
            print(f"  请求失败，跳过")
            continue
        
        # 打印 HTML 长度，确认是否成功获取
        print(f"  获取到 HTML，长度: {len(html)} 字符")
        
        # 打印前500字符预览
        print(f"  HTML 预览: {html[:500]}")
        
        # 提取链接时打印数量
        article_urls = extract_links_from_page(html, list_url)
        print(f"  提取到 {len(article_urls)} 个文章链接")

    return len(new_articles)

# ================= 图形界面 =================
class MonitorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("学校官网监控助手")
        self.root.geometry("1000x600")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # 后台监控相关
        self.running = True
        self.interval_minutes = 30   # 默认间隔
        self.monitor_thread = None

        # 存储所有文章（用于过滤）
        self.all_articles = []

        self.create_widgets()
        self.refresh_article_list(load_articles())   # 加载历史文章
        self.start_monitor_thread()                  # 启动后台定时监控

    def create_widgets(self):
        # ---------- 左侧控制面板 ----------
        left_frame = ttk.Frame(self.root, width=300, relief="ridge", padding=10)
        left_frame.pack(side="left", fill="y", padx=5, pady=5)

        # 关键词管理
        ttk.Label(left_frame, text="关键词管理", font=("Arial", 12, "bold")).pack(anchor="w", pady=5)
        self.keywords_listbox = tk.Listbox(left_frame, height=8)
        self.keywords_listbox.pack(fill="x", pady=5)
        self.refresh_keywords_list()

        kw_frame = ttk.Frame(left_frame)
        kw_frame.pack(fill="x", pady=5)
        self.kw_entry = ttk.Entry(kw_frame, width=15)
        self.kw_entry.pack(side="left", padx=2)
        ttk.Button(kw_frame, text="添加", command=self.add_keyword).pack(side="left", padx=2)
        ttk.Button(kw_frame, text="删除选中", command=self.del_keyword).pack(side="left", padx=2)

        # 监控间隔设置
        ttk.Label(left_frame, text="监控间隔（分钟）").pack(anchor="w", pady=(10,0))
        interval_frame = ttk.Frame(left_frame)
        interval_frame.pack(fill="x", pady=5)
        self.interval_spin = ttk.Spinbox(interval_frame, from_=5, to=1440, width=10, command=self.change_interval)
        self.interval_spin.set(self.interval_minutes)
        self.interval_spin.pack(side="left")
        ttk.Label(interval_frame, text="分钟").pack(side="left", padx=5)

        # 手动检查按钮
        ttk.Button(left_frame, text="立即检查一次", command=self.manual_check).pack(fill="x", pady=10)

        # 打开HTML报告按钮（基于之前的工具生成）
        ttk.Button(left_frame, text="打开HTML报告", command=self.open_html_report).pack(fill="x", pady=5)

        # 状态栏（放在底部，全局）
        self.status_var = tk.StringVar(value="就绪")
        status_bar = ttk.Label(self.root, textvariable=self.status_var, relief="sunken", anchor="w")
        status_bar.pack(side="bottom", fill="x")

        # ---------- 右侧文章列表 ----------
        right_frame = ttk.Frame(self.root, padding=10)
        right_frame.pack(side="right", fill="both", expand=True, padx=5, pady=5)

        ttk.Label(right_frame, text="历史文章（含摘要）", font=("Arial", 12, "bold")).pack(anchor="w")

        # 搜索过滤
        search_frame = ttk.Frame(right_frame)
        search_frame.pack(fill="x", pady=5)
        ttk.Label(search_frame, text="过滤:").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(search_frame, textvariable=self.search_var)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=5)
        ttk.Button(search_frame, text="搜索", command=self.filter_articles).pack(side="left")
        ttk.Button(search_frame, text="重置", command=self.reset_filter).pack(side="left", padx=5)

        # 表格（Treeview）
        columns = ("标题", "摘要", "时间")
        self.tree = ttk.Treeview(right_frame, columns=columns, show="headings", height=20)
        self.tree.heading("标题", text="标题")
        self.tree.heading("摘要", text="摘要")
        self.tree.heading("时间", text="时间")
        self.tree.column("标题", width=200)
        self.tree.column("摘要", width=400)
        self.tree.column("时间", width=120)

        scrollbar = ttk.Scrollbar(right_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.tree.bind("<Double-1>", self.on_article_double_click)

    # 关键词相关方法
    def refresh_keywords_list(self):
        self.keywords_listbox.delete(0, tk.END)
        keywords = load_keywords()
        for kw in keywords:
            self.keywords_listbox.insert(tk.END, kw)

    def add_keyword(self):
        new_kw = self.kw_entry.get().strip()
        if not new_kw:
            return
        keywords = load_keywords()
        if new_kw not in keywords:
            keywords.append(new_kw)
            save_keywords(keywords)
            self.refresh_keywords_list()
            self.kw_entry.delete(0, tk.END)
            self.status_var.set(f"已添加关键词: {new_kw}")
        else:
            messagebox.showinfo("提示", "关键词已存在")

    def del_keyword(self):
        selection = self.keywords_listbox.curselection()
        if not selection:
            return
        idx = selection[0]
        keywords = load_keywords()
        removed = keywords.pop(idx)
        save_keywords(keywords)
        self.refresh_keywords_list()
        self.status_var.set(f"已删除关键词: {removed}")

    def change_interval(self):
        try:
            val = int(self.interval_spin.get())
            if val >= 5:
                self.interval_minutes = val
                self.status_var.set(f"监控间隔已改为 {val} 分钟")
            else:
                self.interval_spin.set(30)
        except:
            pass

    def manual_check(self):
        """手动触发一次检查（在后台线程运行，避免界面卡顿）"""
        self.status_var.set("手动检查中...")
        threading.Thread(target=self._do_check, daemon=True).start()

    def _do_check(self):
        """在后台线程中执行检查，完成后更新界面并弹窗提醒"""
        try:
            # 执行检查，获取新文章数量
            new_count = run_once(gui_callback=self._on_new_articles)
            # 检查成功：更新状态并弹窗
            self.root.after(0, lambda: self._show_check_result(True, new_count))
        except Exception as e:
            # 检查失败：记录错误信息
            error_msg = str(e)
            self.root.after(0, lambda: self._show_check_result(False, error_msg=error_msg))

    def append_articles(self, new_articles):
        """新文章追加到列表"""
        self.all_articles = load_articles()   # 重新加载全部
        self.refresh_article_list(self.all_articles)
        # 滚动到最后一条（最新）
        if self.tree.get_children():
            self.tree.see(self.tree.get_children()[-1])

    def refresh_article_list(self, articles):
        """刷新树视图（完全替换）"""
        self.all_articles = articles
        self.tree.delete(*self.tree.get_children())
        for art in articles:
            summary_short = art['summary'][:80] + "..." if len(art['summary']) > 80 else art['summary']
            self.tree.insert("", tk.END, values=(art['title'], summary_short, art['time']), tags=(art['url'],))

    def filter_articles(self):
        keyword = self.search_var.get().strip().lower()
        if not keyword:
            self.refresh_article_list(self.all_articles)
            return
        filtered = [art for art in self.all_articles if keyword in art['title'].lower() or keyword in art['summary'].lower()]
        self.tree.delete(*self.tree.get_children())
        for art in filtered:
            summary_short = art['summary'][:80] + "..." if len(art['summary']) > 80 else art['summary']
            self.tree.insert("", tk.END, values=(art['title'], summary_short, art['time']), tags=(art['url'],))

    def reset_filter(self):
        self.search_var.set("")
        self.refresh_article_list(self.all_articles)

    def on_article_double_click(self, event):
        """双击打开链接"""
        selected = self.tree.selection()
        if not selected:
            return
        url = self.tree.item(selected[0], "tags")[0]
        import webbrowser
        webbrowser.open(url)

    def open_html_report(self):
        """如果之前生成了HTML报告，用浏览器打开"""
        report_path = "关键词监控报告.html"
        if os.path.exists(report_path):
            import webbrowser
            webbrowser.open(report_path)
        else:
            messagebox.showinfo("提示", "报告文件不存在，请先手动执行一次检查生成报告")

    def start_monitor_thread(self):
        """启动后台定时监控线程"""
        def monitor_loop():
            while self.running:
                time.sleep(self.interval_minutes * 60)
                if not self.running:
                    break
                # 执行检查，不传递回调（自动保存文件，界面会定时刷新）
                run_once()
                # 定时刷新界面（重新加载文章文件）
                self.root.after(0, self._refresh_from_storage)
        self.monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
        self.monitor_thread.start()

    def _refresh_from_storage(self):
        articles = load_articles()
        self.refresh_article_list(articles)
        self.status_var.set(f"自动检查完成，共 {len(articles)} 条记录")

    def on_close(self):
        self.running = False
        self.root.destroy()

    def _on_new_articles(self, new_articles):
        """当 run_once 发现新文章时回调（已在 run_once 内调用）"""
        self.root.after(0, lambda: self.append_articles(new_articles))

    def _show_check_result(self, success, new_count=0, error_msg=""):
        """在主线程中显示检查结果"""
        if success:
            self.status_var.set(f"手动检查完成，发现 {new_count} 篇新文章")
            if new_count > 0:
                messagebox.showinfo("检查完成", f"共发现 {new_count} 篇包含关键词的新文章，已添加到列表并发送通知。")
            else:
                messagebox.showinfo("检查完成", "没有发现新的包含关键词的文章。")
        else:
            self.status_var.set(f"手动检查出错: {error_msg[:50]}")
            messagebox.showerror("检查出错", f"检查过程中发生错误：\n{error_msg}\n请检查网络或学校网址是否正确。")

# ================= 程序入口 =================
if __name__ == "__main__":
    root = tk.Tk()
    app = MonitorApp(root)
    root.mainloop()