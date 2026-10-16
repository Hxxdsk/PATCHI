# -*- coding: utf-8 -*-
"""帕琪工坊 · 可选的 Windows 本地浏览器自动化助手。

启动后仅监听 127.0.0.1:8765，使用系统 Edge/Chrome + Playwright 模拟正规网页操作。
不调用隐藏接口，不绕过网站验证码或访问限制；实际页面结构变化可能导致自动模式失败。
AI 抠图通过已有浏览器访问网站；不提供官方 API 代理。
"""
import base64
import json
import os
import re
import tempfile
import shutil
import threading
import webbrowser
import uuid
import datetime
import traceback
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

HOST, PORT = '127.0.0.1', 8765
AUTOMATION_PROTOCOL = 13
# Third-party sites may show ads and unrelated links; automated clicks must remain in the tool.
ALLOWED_HOSTS = frozenset(('koukoutu.com', 'www.koukoutu.com'))
SITE = 'https://www.koukoutu.com/removebgtool/all'
LIMIT = 45 * 1024 * 1024
workdir = tempfile.TemporaryDirectory(prefix='patchi_bg_')
lock = threading.RLock()
queue = []
busy = False
finished = False
stop_flag = False
message = '助手就绪：后台同一页面顺序上传、抠图、下载、回填；不会新开空白守护窗口'
# 独立于系统 Edge/Chrome 普通配置文件，避免 Playwright 争抢用户正在使用的浏览器。
# Cookies、LocalStorage、IndexedDB 由 Chromium 自动保存，退出助手也不会被清除。
APP_DATA = Path(os.environ.get('LOCALAPPDATA') or os.environ.get('XDG_DATA_HOME') or (Path.home() / '.local' / 'share'))
PROFILE_DIR = APP_DATA / '帕琪工坊' / '抠图浏览器登录状态'
login_active = False
login_message = '尚未打开登录窗口；首次使用请先登录一次'
login_finish = threading.Event()



class LoginRequired(RuntimeError):
    """网站显式要求登录；不能自动绕过。"""


class BrowserSessionLost(RuntimeError):
    """浏览器进程或持久上下文已经失效；需要重新启动，而不是重开任务标签。"""


def is_browser_context_alive(context):
    """检查真实浏览器连接，而不只检查当前任务标签。"""
    if context is None:
        return False
    try:
        browser = context.browser
        if browser is not None and not browser.is_connected():
            return False
        context.pages  # 上下文关闭时这里可能直接抛错
        return True
    except Exception:
        return False


def is_browser_disconnected(exc):
    """兼容 Playwright 不同版本的 TargetClosedError 文案。"""
    msg = str(exc).lower()
    return isinstance(exc, BrowserSessionLost) or any(term in msg for term in (
        'target page, context or browser has been closed',
        'browser has been closed', 'browser closed', 'browser disconnected',
        'browsercontext.new_page: target', 'connection closed',
    ))


def ensure_browser_guard(context):
    """V11：不再创建任何 about:blank 守护页面。"""
    if not is_browser_context_alive(context):
        raise BrowserSessionLost('浏览器连接已中断')
    return next((p for p in context.pages if not p.is_closed()), None)


def restart_saved_browser(playwright, previous=None, visible=False):
    """使用同一个持久会话，失败时恢复；整批只使用一个任务标签。"""
    if previous is not None:
        try: previous.close()
        except Exception: pass
    last_error = None
    for attempt in range(1, 4):
        try:
            if attempt > 1: time.sleep(.8 * attempt)
            context = launch_saved_browser(playwright, visible=visible)
            print('[会话] 浏览器已启动，复用已保存的网站登录状态', flush=True)
            return context
        except Exception as exc:
            last_error = exc
            print(f'[会话恢复] {attempt}/3：{str(exc)[:210]}', flush=True)
    raise BrowserSessionLost('浏览器启动失败：' + str(last_error)[:220])


def downloads_directory():
    """Windows Downloads known folder (localized Explorer name supported)."""
    configured = os.environ.get('PAQI_DOWNLOAD_DIR')
    if configured:
        path = Path(configured).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    if os.name == 'nt':
        try:
            import winreg
            key=r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders'
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                value=winreg.QueryValueEx(k, '{374DE290-123F-4565-9164-39C4925E467B}')[0]
            path=Path(os.path.expandvars(value)).expanduser()
            if path.exists() or path.parent.exists():
                path.mkdir(parents=True,exist_ok=True)
                return path
        except Exception as exc:
            print('[下载目录] 系统目录查询失败:',str(exc)[:120],flush=True)
    p=Path.home()/'Downloads'
    p.mkdir(parents=True,exist_ok=True)
    return p

DOWNLOAD_DIR=downloads_directory()
BATCH_TAG=datetime.datetime.now().strftime('%Y%m%d_%H%M%S')


def unique_download_path(job, index):
    stem=re.sub(r'[^\w\u4e00-\u9fff-]+','_',Path(job['name']).stem)[:48].strip('_') or 'image'
    root=f'帕琪工坊_{BATCH_TAG}_{index:03d}_{stem}_透明'
    output=DOWNLOAD_DIR/(root+'.png')
    counter=2
    while output.exists():
        output=DOWNLOAD_DIR/(f'{root}_{counter}.png')
        counter+=1
    return output


def login_detected(page):
    """Global login nav is normal; detect ONLY contextual forced-login messages/dialogs."""
    try:
        matches=page.locator('[role="dialog"], .ant-modal, .el-dialog, .modal, .login-modal, .login-dialog')
        for i in range(min(matches.count(),8)):
            v=matches.nth(i)
            if not v.is_visible(timeout=200): continue
            t=v.inner_text(timeout=500)[:500]
            if re.search(r'(?:请先|需要|必须|登录后|注册后|完成登录|扫码登录).{0,12}(?:登录|注册|下载|抠图)|(?:登录|注册).{0,12}(?:后才能|后下载)',t):
                return t[:150]
        # e.g. message/toast from page after click
        for pat in (r'请先登录',r'登录后下载',r'登录后才能',r'需要登录才能',r'请登录后'):
            text=page.get_by_text(re.compile(pat))
            for i in range(min(text.count(),3)):
                if text.nth(i).is_visible(timeout=150):return text.nth(i).inner_text(timeout=200)[:120]
    except Exception:pass
    return None


def check_login(page):
    reason=login_detected(page)
    if reason:raise LoginRequired('网站要求登录。请先在工具里打开登录窗口、正常登录并保存后重试：'+reason)


def api_state():
    with lock:
        return {'busy': busy, 'finished': finished, 'message': message,
                'downloadDir':str(DOWNLOAD_DIR), 'loginActive':login_active, 'loginMessage':login_message, 'jobs': [{**{k: j[k] for k in ('id','name','status','message')}, 'downloadName':Path(j['result']).name if j.get('result') else ''} for j in queue]}


def set_job(job, status, detail=''):
    with lock:
        job['status'] = status
        job['message'] = detail


def has_alpha_png(p):
    raw = Path(p).read_bytes()
    return validate_transparent_png(raw)


def validate_transparent_png(raw):
    import io
    from PIL import Image
    if not raw.startswith(b'\x89PNG\r\n\x1a\n') or len(raw) > 65 * 1024 * 1024:
        raise ValueError('下载结果不是有效 PNG')
    with Image.open(io.BytesIO(raw)) as image:
        if image.width * image.height > 32_000_000:
            raise ValueError('下载图片超过 3200 万像素')
        image.verify()
    with Image.open(io.BytesIO(raw)) as image:
        if 'A' not in image.getbands() and 'transparency' not in image.info:
            raise ValueError('下载 PNG 没有透明通道')
        alpha = image.convert('RGBA').getchannel('A')
        low, high = alpha.getextrema()
        if low == 255 or high == 0:
            raise ValueError('下载 PNG 没有有效抠图结果，请检查网站下载格式')
    return raw


def safe_site_url(url):
    """放宽到站内页面：允许同站正常流程跳转，仍阻止站外广告和下载客户端页。"""
    try:
        p = urlparse(url)
        host = (p.hostname or '').lower()
        path = p.path or '/'
        if p.scheme != 'https' or host not in ALLOWED_HOSTS:
            return False
        blocked = (r'/download(?:client|app|software|helper)', r'/soft(?:ware)?/', r'/client(?:/|$)', r'/apk(?:/|$)')
        return not any(re.search(pat, path, re.I) for pat in blocked)
    except (ValueError, TypeError):
        return False


def assert_on_site(page):
    if page.is_closed():
        raise RuntimeError('自动化页面被关闭，请不要在任务期间关闭抠图浏览器')
    if not safe_site_url(page.url):
        raise RuntimeError('自动化页面意外跳转，已安全停止：' + str(page.url)[:150])


def install_navigation_guard(context, page):
    """只作用于当前任务标签；手动登录窗口不受限制，可跳转官方认证页。"""
    download_guard = {'until': 0.0}
    setattr(page, '_patchi_download_guard', download_guard)

    def route_guard(route):
        req = route.request
        uri = req.url
        # 只有点击已确认的结果下载按钮后，才允许触发 CDN 导航；仍会验证返回文件。
        authorized_download = (time.monotonic() < download_guard['until']
                               and uri.startswith('https://')
                               and req.resource_type == 'document')
        if req.is_navigation_request() and not (
            safe_site_url(uri) or uri.startswith(('about:', 'blob:', 'data:')) or authorized_download
        ):
            print('[安全拦截] 当前任务非目标页面导航:', uri[:160], flush=True)
            route.abort()
        else:
            route.fallback()
    page.route('**/*', route_guard)


def launch_saved_browser(playwright, visible=False):
    """后台运行复用工坊持久站点数据；人工登录窗口才可见。

    不占用或复制正在运行的 Edge/Chrome 默认用户目录（可能导致 profile 锁定、损坏）。
    浏览器数据均存放在原有的帕琪工坊登录目录，不主动清空。
    """
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    headless = not visible and os.environ.get('PAQI_SHOW_AUTOMATION') != '1'
    errors = []
    browser_bin = os.environ.get('PAQI_BROWSER_BIN')
    choices = [None, 'msedge', 'chrome'] if browser_bin else ['msedge', 'chrome']
    for channel in choices:
        try:
            session_downloads = Path(workdir.name) / ('browser_downloads_' + uuid.uuid4().hex[:12])
            session_downloads.mkdir(parents=True,exist_ok=True)
            opts = {
                'user_data_dir':str(PROFILE_DIR),
                'headless':headless,
                'accept_downloads':True,
                'downloads_path':str(session_downloads),
                'viewport':{'width':1600,'height':960},
                'device_scale_factor':1,
                'args':['--no-first-run','--no-default-browser-check'],
            }
            if channel: opts['channel']=channel
            if browser_bin and channel is None:
                opts['executable_path'] = browser_bin
            context = playwright.chromium.launch_persistent_context(**opts)
            setattr(context,'_patchi_download_dir',session_downloads)
            setattr(context,'_patchi_profile_label',str(PROFILE_DIR))
            print('[复用登录数据]',PROFILE_DIR,'后台' if headless else '可见',flush=True)
            return context
        except Exception as exc:
            errors.append(str(exc)[:240])
            print('[启动浏览器候选失败]',channel,errors[-1],flush=True)
    raise RuntimeError('无法启动抠图工作浏览器，可能浏览器登录窗口仍在使用相同配置目录：' + (errors[-1] if errors else ''))


def login_browser_worker():
    """用户亲自完成官网认证；不能替代登录，也不访问登录验证码。"""
    global login_active, login_message
    context = None
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            context = launch_saved_browser(playwright, visible=True)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(SITE, wait_until='domcontentloaded', timeout=60000)
            with lock:
                login_message = '登录窗口已打开，请在网站上正常登录，再点「完成登录并保存」'
            while not login_finish.is_set():
                if not any(not pg.is_closed() for pg in context.pages):
                    break
                time.sleep(.5)
    except Exception as e:
        with lock:
            login_message = '登录窗口打开失败：' + str(e)[:220]
        print('[登录窗口失败]', str(e), flush=True)
    finally:
        if context:
            try:
                context.close()  # Chromium 自动将网站状态写入专用 profile
            except Exception as e:
                print('[关闭登录浏览器]', str(e)[:100], flush=True)
        with lock:
            login_active = False
            if not login_message.startswith('登录窗口打开失败'):
                login_message = '浏览器已关闭，登录状态已保存在电脑；可启动自动抠图（具体是否有效以网站为准）'
        login_finish.clear()


def is_safe_action(element, page, download=False):
    """按网页元素判断，不依赖操作系统缩放或屏幕坐标。"""
    try:
        info = element.evaluate("""e => {
            const r = e.getBoundingClientRect();
            const a = e.closest('a[href]');
            return {
                href: a ? a.href : '',
                inChrome: !!e.closest('header, footer, nav, [role=navigation], aside'),
                visible: r.width > 0 && r.height > 0,
                disabled: e.disabled || e.getAttribute('aria-disabled') === 'true',
                text: (e.innerText || e.getAttribute('aria-label') || e.getAttribute('title') || '').trim().slice(0, 120)
            };
        }""")
        if not info['visible'] or info['inChrome'] or info['disabled']:
            return False
        href = info['href']
        if href and not (safe_site_url(href) or href.startswith(('blob:', 'data:'))):
            # Only explicit download controls may point directly at an image CDN.
            if not (download and href.startswith('https://') and
                    re.search(r'(?:\.(?:png|zip)(?:[?#]|$)|/download(?:[/?#]|$))', href, re.I)):
                return False
        return True
    except Exception:
        return False


START_LABEL = re.compile(r'^(?:开始抠图|立即抠图|开始处理|立即处理|一键抠图|开始去背景)$', re.I)
# 官网按钮右上角可能叠加「免费」角标，也可能包含图标、换行或者子节点。
# 只对精确指定的「结果图下载」文字容错，不能误点「下载客户端」或「下载成功图片」。
DOWNLOAD_LABEL = re.compile(
    r'^(?:下载高清大图(?:[（(]PNG[)）])?|高清(?:图片|大图)?下载(?:[（(]PNG[)）])?'
    r'|下载高清图(?:[（(]PNG[)）])?|下载(?:透明)?PNG(?:图片)?'
    r'|下载原图|下载图片|免费下载(?:高清)?(?:PNG)?|免费(?:高清)?下载(?:PNG)?'
    r'|Download(?:HD|Image|PNG|HighResolution)?)$', re.I)
PROMO_SUFFIX = re.compile(r'(?:免费|限免|FREE|HOT|NEW|推荐|PNG格式|透明背景){1,3}$', re.I)


def normalized_label(value):
    """消除 SVG 的换行及常见零宽字符，普通 HTML 缩放与屏幕 DPI 无关。"""
    return re.sub(r'[\s\u200b\u200d\u2060\ufeff]+', '', str(value or ''))


def matches_control_label(value, label):
    raw = normalized_label(value).strip()
    if not raw:
        return False
    if label.fullmatch(raw):
        return True
    if label is DOWNLOAD_LABEL:
        # 仅去除按钮开头的箭头符号，不做泛化的包含「下载」匹配。
        raw = re.sub(r'^[⬇↓⇩↧⭳⇣⤓⇩📥⏬🎁]+', '', raw)
        if label.fullmatch(raw):
            return True
        clean = PROMO_SUFFIX.sub('', raw)
        if label.fullmatch(clean):
            return True
    return False


def visible_controls(page, label, exact_text=True):
    """读取 DOM 元素与子标签；网页缩放和免费角标都不应影响结果按钮识别。"""
    seen = set()
    finder = (re.compile(r'下载|Download', re.I) if label is DOWNLOAD_LABEL
              else re.compile(r'开始|抠图|处理', re.I))
    sources = [
        page.get_by_role('button', name=finder),
        page.locator('button, a, [role="button"], [onclick], [class*="download" i], [class*="btn" i]').filter(has_text=finder),
        page.get_by_text(finder),
    ]
    for source in sources:
        try:
            count = min(source.count(), 65)
        except Exception:
            continue
        for i in range(count):
            ele = source.nth(i)
            try:
                if not ele.is_visible(timeout=400) or not ele.is_enabled(timeout=400):
                    continue
                # 取实际可点击元素，不能因为页面中某处有下载文字就点击整个卡片。
                actionable = ele.locator(
                    'xpath=ancestor-or-self::*[self::button or self::a or @role="button" or @onclick '
                    'or contains(translate(@class,"DOWNLOADBTN","downloadbtn"),"download") '
                    'or contains(translate(@class,"BTN","btn"),"btn")][1]'
                )
                if not actionable.count():
                    # React/Vue 可能在没有 button/onclick/class=download 的 div 上绑定单击事件。
                    # 只接受自身或近邻明确具有 pointer 光标、且文案足够短的节点。
                    inherited_pointer = None
                    for level in range(5):
                        ancestor = ele.locator('xpath=' + '/'.join(['..'] * level)) if level else ele
                        try:
                            props = ancestor.evaluate("""e => ({
                                cursor: getComputedStyle(e).cursor,
                                ownCursor: e.style && e.style.cursor === 'pointer',
                                tag: e.tagName.toLowerCase(),
                                length: (e.innerText || '').trim().length,
                                w: e.getBoundingClientRect().width,
                                h: e.getBoundingClientRect().height,
                            })""")
                        except Exception:
                            break
                        if (props['cursor'] == 'pointer' and props['tag'] not in ('body','html','main','section','article')
                                and props['length'] <= 90 and props['w'] > 0 and props['h'] > 0):
                            if props['ownCursor']:
                                actionable = ancestor
                                break
                            if inherited_pointer is None:
                                inherited_pointer = ancestor
                    if not actionable.count() and inherited_pointer is not None:
                        actionable = inherited_pointer
                if not actionable.count():
                    continue
                target = actionable.first
                if not target.is_visible(timeout=300) or not is_safe_action(target, page, download=label is DOWNLOAD_LABEL):
                    continue
                label_parts = target.evaluate("""e => {
                    let value = [e.innerText || '', e.textContent || '', e.getAttribute('aria-label') || '', e.getAttribute('title') || ''];
                    for (const child of e.querySelectorAll('span,b,strong,small,em,label,div')) {
                        // 只读当前按钮内部的短标签，不遍历整张页面的大块内容。
                        const t = child.innerText || child.textContent || '';
                        if (t.trim().length <= 45) value.push(t);
                    }
                    return value.slice(0,60);
                }""")
                full_text = normalized_label(label_parts[0])
                if len(full_text) > 90:
                    continue
                if not any(matches_control_label(part, label) for part in label_parts):
                    continue
                ident = target.evaluate('e => {const r=e.getBoundingClientRect();return [Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)].join(",");}')
                if ident in seen:
                    continue
                seen.add(ident)
                yield target
            except Exception:
                continue



# Browser DOM driven result-button discovery. The site can render clickable Vue/React divs
# instead of native buttons, and its orange 免费 badge may be inside or outside the button.
# Work from direct text fragments first; reject header/menu items, then rank result-side actions.
DOWNLOAD_PROBE_JS = r"""() => {
    const normalize = t => String(t || '').replace(/[\s\u200b\u200d\ufeff]/g, '');
    const valid = t => {
      const s = normalize(t);
      if (/下载客户端|下载成功图片|批量下载|全部下载|下载历史|下载软件|客户端下载|下载助手|downloadclient/i.test(s)) return false;
      return /下载高清大图|下载高清图|高清大图下载|下载透明PNG|下载PNG|下载原图|免费下载|Download(?:HD|PNG|Image|High)/i.test(s)
             || (/下载/.test(s) && /高清/.test(s) && s.length < 95);
    };
    const showing = e => {
      if (!(e instanceof HTMLElement) && !(e instanceof SVGElement)) return false;
      const r = e.getBoundingClientRect();
      const cs = getComputedStyle(e);
      return r.width > 1 && r.height > 1 && cs.display !== 'none'
        && cs.visibility !== 'hidden' && Number(cs.opacity) > .05;
    };
    const nodes = [];
    function collect(root) {
      for (const e of root.querySelectorAll('*')) {
        nodes.push(e);
        if (e.shadowRoot) collect(e.shadowRoot);
      }
    }
    collect(document.body);
    // A real rendered success marker must be visible before auto-clicking any download control.
    // Do not mistake a global '下载成功图片' toolbar for an image result.
    const successMarkers = nodes.filter(e => {
      if (!showing(e)) return false;
      const s = normalize(e.innerText || e.textContent || '');
      return s.length < 35 && /^(抠图成功|抠图完成|处理完成|去背景成功|去背成功|全部完成)[!！]?$/.test(s);
    }).map(e => {const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};});
    const resultImgs = nodes.filter(e => e.tagName === 'IMG' && showing(e) && e.getBoundingClientRect().width > 140 && e.getBoundingClientRect().height > 140 && e.getBoundingClientRect().x > innerWidth*.37).length;
    const pool = new Map();
    for (const e of nodes) {
      if (!showing(e)) continue;
      const tag = e.tagName.toLowerCase();
      if (['html','body','main','header','footer','nav','aside','section'].includes(tag)) continue;
      const directText = [...e.childNodes].filter(n => n.nodeType === 3).map(n=>n.textContent).join('');
      const a11y = [e.getAttribute('aria-label'),e.getAttribute('title'),e.getAttribute('data-title')].filter(Boolean).join(' ');
      const full = (e.innerText || '').trim();
      const mainText = directText.trim() || a11y || ((tag === 'button'||tag === 'a'||e.getAttribute('role') === 'button') ? full : '');
      if (!valid(mainText)) continue;
      if (normalize(mainText).length > 170) continue;
      let clickable = e.closest('button,a,[role="button"],[onclick],[class*="download" i],[class*="btn" i]');
      if (!clickable || ['BODY','HTML','MAIN','HEADER','FOOTER','NAV','ASIDE','SECTION'].includes(clickable.tagName) || (clickable.innerText||'').length > 240) {
        clickable=null;
        let cur=e;
        for (let n=0;cur && n<6;n++,cur=cur.parentElement) {
          const r=cur.getBoundingClientRect();
          if (r.width>1 && r.height>1 && r.width<440 && r.height<105 && getComputedStyle(cur).cursor==='pointer') {clickable=cur;break;}
        }
      }
      if (!clickable) {
        // Deliberately allow ordinary div/span handlers when the result is visually confirmed.
        // The nested label might be the only DOM node with a distinctive CSS class.
        let cur=e;
        for (let n=0;cur && n<4;n++,cur=cur.parentElement) {
          const r=cur.getBoundingClientRect();
          if (r.width>55 && r.width<440 && r.height>16 && r.height<105 && !['SVG','PATH'].includes(cur.tagName)) {
            clickable=cur;break;
          }
        }
      }
      if (!clickable || !showing(clickable) || clickable.closest('header,footer,nav,aside,[role="navigation"]')) continue;
      const link=clickable.closest('a[href]');
      const href=link ? link.href : '';
      if (href && !(/^(https:\/\/|blob:|data:)/i.test(href))) continue;
      const r=clickable.getBoundingClientRect();
      if (r.width>980 || r.height>160) continue;
      const label=normalize(mainText).slice(0,150);
      let score=0;
      if (/下载高清大图/.test(label)) score+=120;
      else if (/下载高清|高清.*下载/.test(label)) score+=105;
      else score+=75;
      if (clickable.matches('button,a,[role="button"]')) score+=30;
      if (getComputedStyle(clickable).cursor==='pointer') score+=15;
      if (r.x + r.width/2 > innerWidth*.50) score+=30;
      if (successMarkers.length) {
        const cx=r.x+r.width/2,cy=r.y+r.height/2;
        let near=Math.min(...successMarkers.map(m=>Math.abs(m.x-cx)*.55+Math.abs(m.y-cy)*.20));
        if(near<680) score+=25;
        else if (near<1050) score+=10;
      }
      if (/result|success|output|完成|结果|下载/i.test(String(clickable.className||''))) score+=12;
      if (normalize(clickable.innerText||'').length > 140) score-=100;
      const old=pool.get(clickable);
      if (!old || score>old.score) pool.set(clickable,{node:clickable,label,score,rect:{x:r.x,y:r.y,w:r.width,h:r.height},href});
    }
    // Clear prior candidate marks on each new probe, and only choose after confirmed result.
    for (const e of nodes) if(e.hasAttribute('data-paqi-download-target'))e.removeAttribute('data-paqi-download-target');
    const ready=successMarkers.length>0 || (resultImgs>0 && [...pool.values()].some(c=>c.score>155));
    const ranked=[...pool.values()].sort((a,b)=>b.score-a.score);
    if (ready) ranked.slice(0,10).forEach((item,i)=>item.node.setAttribute('data-paqi-download-target',String(i)));
    return {ready,successMarkers:successMarkers.length,resultImgs,
      candidates:ranked.slice(0,12).map((o,i)=>({index:i,label:o.label,score:o.score,rect:o.rect,href:o.href.slice(0,140)}))};
}"""


def probe_download_candidates(page):
    """DOM probe returning precise marked locators and diagnostic candidate descriptions."""
    try:
        info = page.evaluate(DOWNLOAD_PROBE_JS)
    except Exception as ex:
        return [], {'ready':False,'error':str(ex)[:200],'candidates':[]}
    if not info.get('ready'):
        return [], info
    targets=[]
    for c in info.get('candidates', [])[:10]:
        if c['index']>=16 or c['score']<72:
            continue
        locator=page.locator('[data-paqi-download-target="%s"]' % c['index']).first
        try:
            if locator.count() and is_safe_action(locator,page,download=True):
                targets.append((locator,c))
        except Exception:
            continue
    return targets,info


def download_controls(page):
    """Prefer direct rendered result controls, retain the older Playwright selector fallback."""
    targets,info=probe_download_candidates(page)
    if targets:
        return [loc for loc,_ in targets[:10]]
    if not info.get('ready'):
        return []
    return list(visible_controls(page,DOWNLOAD_LABEL))[:8]

def upload_to_site(page, path):
    """把本地图片送进真实文件输入框，绝不模糊点击文字/链接。"""
    assert_on_site(page)
    inputs = page.locator('input[type="file"]')
    for i in range(min(inputs.count(), 12)):
        ele = inputs.nth(i)
        accepts = (ele.get_attribute('accept') or '').lower()
        if accepts and not any(x in accepts for x in ('image', '.jpg', '.jpeg', '.png', '.webp', '*')):
            continue
        try:
            ele.set_input_files(str(path), timeout=12000)
            assert_on_site(page)
            return
        except Exception:
            continue
    # 某些站点点击上传按钮时才动态生成真正的 <input type=file>。
    for control in visible_controls(page,re.compile(r'^(?:上传图片|选择图片|添加图片|上传文件)$')):
        try:
            with page.expect_file_chooser(timeout=5000) as dialog:
                control.click(timeout=4000)
            dialog.value.set_files(str(path))
            assert_on_site(page)
            return
        except Exception:
            assert_on_site(page)
    raise RuntimeError('未找到网站真实文件上传控件，安全停止；没有点击页面广告')


def text_exists(page, exact_text):
    try:
        return page.get_by_text(exact_text, exact=True).first.is_visible(timeout=250)
    except Exception:
        return False


def result_visible(page):
    """检查当前页面是否有完成结果（连续多图时上层必须防止读取上一张）。"""
    assert_on_site(page)
    if download_controls(page):
        return True
    for marker in ('抠图成功', '处理完成', '抠图完成', '全部完成', '去背景成功'):
        if _visible_text(page, r'^\s*' + re.escape(marker) + r'\s*[!！]?\s*$') is not None:
            return True
    return False


def scroll_for_controls(page, tick):
    """激活懒加载/折叠结果区域，页面缩放不同也能定位元素。"""
    try:
        page.evaluate("""step => {
            const h = Math.max(500, window.innerHeight * .85);
            if (step % 5 === 0) window.scrollTo(0, 0);
            else window.scrollBy(0, h);
        }""", tick)
    except Exception:
        pass


def start_processing(page, job):
    deadline = time.monotonic() + 65
    set_job(job, 'running', '已上传，查找开始抠图操作')
    tick = 0
    while time.monotonic() < deadline:
        assert_on_site(page)
        check_login(page)
        for ctl in visible_controls(page, START_LABEL):
            set_job(job, 'running', '点击开始抠图')
            try:
                ctl.scroll_into_view_if_needed(timeout=4000)
                ctl.click(timeout=7500)
                assert_on_site(page)
                set_job(job, 'running', '已开始抠图，等待完成')
                return 'clicked'
            except Exception as e:
                assert_on_site(page)
                print('[启动抠图]', str(e)[:150], flush=True)
        # 有的网站上传后即自动生成；上层会检查是否误读上一张结果。
        if result_visible(page):
            return 'auto'
        with lock:
            if stop_flag:
                raise RuntimeError('用户停止任务')
        tick += 1
        if tick % 5 == 0:
            scroll_for_controls(page, tick)
        page.wait_for_timeout(850)
    raise RuntimeError('上传后未找到开始抠图/自动完成。请在专用浏览器登录后重试，或查看诊断截图')


def wait_for_result(page, job, seconds=210):
    deadline = time.monotonic() + seconds
    prev = None
    tick = 0
    while time.monotonic() < deadline:
        with lock:
            if stop_flag:
                raise RuntimeError('用户停止任务')
        assert_on_site(page)
        check_login(page)
        if result_visible(page):
            set_job(job, 'running', '检测到完成结果，查找 PNG 下载按钮')
            return
        stage = '等待网站生成结果（将自动查找页面下方按钮）'
        if stage != prev:
            set_job(job, 'running', stage)
            prev = stage
        tick += 1
        if tick % 5 == 0:
            scroll_for_controls(page, tick)
        page.wait_for_timeout(1200)
    raise RuntimeError('结果等待超时，请查看失败诊断图；可能是网站登录或服务限制')


def extract_download(download, output_path):
    suggested = (download.suggested_filename or '').lower()
    if suggested.endswith(('.exe','.msi','.html','.htm','.ttf','.otf','.pdf')):
        raise RuntimeError('网站返回非图片文件：'+suggested)
    candidate = Path(output_path).with_suffix('.download')
    download.save_as(str(candidate))
    raw = candidate.read_bytes()
    if raw.startswith(b'\x89PNG\r\n\x1a\n'):
        candidate.replace(output_path)
        return has_alpha_png(output_path)
    if raw.startswith(b'PK\x03\x04'):
        import io, zipfile
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names = [n for n in z.namelist() if n.lower().endswith('.png') and not n.startswith('__MACOSX/')]
            # Sequential one-picture mode: accept a ZIP only when mapping is unambiguous.
            if len(names) != 1:
                raise RuntimeError('下载 ZIP 含多张 PNG，无法安全对应到当前素材')
            info = z.getinfo(names[0])
            if info.file_size > 60 * 1024 * 1024:
                raise RuntimeError('ZIP 内图片超过60MB')
            Path(output_path).write_bytes(z.read(info))
        candidate.unlink(missing_ok=True)
        return has_alpha_png(output_path)
    candidate.unlink(missing_ok=True)
    raise RuntimeError('下载结果不是透明 PNG 或单张 PNG 的 ZIP')



DOWNLOAD_CONFIRM_TIMEOUT = 32
DOWNLOAD_CLICK_ERROR_TIMEOUT = 12
DOWNLOAD_DIALOG_TIMEOUT = 18

# Capture the same transient blob anchor used by FileSaver, only while downloading.
DOWNLOAD_CAPTURE_JS = r"""(() => {
  if(window.__patchiDownloadCapture)return;
  const state={active:false,epoch:0,files:[],pending:new Set(),urls:new Set()};
  const original=HTMLAnchorElement.prototype.click;
  const dispatch=HTMLAnchorElement.prototype.dispatchEvent;
  const capture=anchor=>{
    const href=anchor.href,epoch=state.epoch;
    if(state.active&&anchor.hasAttribute('download')&&/^(blob:|data:)/i.test(href)&&state.pending.size<3&&!state.urls.has(href)){
      state.urls.add(href);
      const task=(async()=>{
        try {
          const response=await fetch(href),blob=await response.blob();
          if(blob.size>65*1024*1024||blob.size<45)return;
          const data=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result);r.onerror=reject;r.readAsDataURL(blob)});
          if(state.epoch===epoch&&state.active&&state.files.length<3)state.files.push(data);
        } catch(_) {}
      })();
      state.pending.add(task);task.finally(()=>state.pending.delete(task));
    }
  };
  HTMLAnchorElement.prototype.click=function(...args){capture(this);return original.apply(this,args)};
  HTMLAnchorElement.prototype.dispatchEvent=function(event){if(event.type==='click')capture(this);return dispatch.call(this,event)};
  window.__patchiDownloadCapture={begin(){state.epoch++;state.active=true;state.files=[];state.urls.clear()},take(){return state.files.splice(0)},end(){state.active=false;state.epoch++;state.files=[];state.urls.clear()}};
})();"""


class DownloadAcknowledgedWithoutFile(RuntimeError):
    """站点显示下载成功，但未检测到可用的本地 PNG；禁止在弹窗上继续乱点。"""


def site_download_acknowledged(page):
    """严格识别「下载成功！」弹窗标题，不把顶部「下载成功图片」入口误认成完成。"""
    try:
        if page.is_closed():
            return False
        heading = page.get_by_text(re.compile(r'^\s*下载成功\s*[!！]?\s*$'))
        for i in range(min(heading.count(), 6)):
            if heading.nth(i).is_visible(timeout=180):
                return True
    except Exception:
        pass
    return False


def _snapshot_downloads(folder):
    """记录单张点击前的文件状态，避免误拾旧文件。"""
    state = {}
    if not folder or not folder.is_dir():
        return state
    try:
        for path in folder.iterdir():
            if not path.is_file() or path.is_symlink():
                continue
            st = path.stat()
            state[path.name] = (st.st_mtime_ns, st.st_size)
    except OSError:
        pass
    return state


def _read_download_image(data):
    """返回完整的透明 PNG 字节；支持单 PNG 的 ZIP，绝不信任文件扩展名。"""
    import io, zipfile
    if len(data) > 65 * 1024 * 1024:
        raise ValueError('单个下载文件超过65MB')
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        png = data
    elif data.startswith(b'PK\x03\x04'):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = [v for v in archive.infolist() if not v.is_dir() and v.filename.lower().endswith('.png')]
            if len(names) != 1 or names[0].file_size > 60 * 1024 * 1024:
                raise ValueError('ZIP 不是单张透明 PNG')
            png = archive.read(names[0])
    else:
        raise ValueError('下载不是 PNG/ZIP')
    return validate_transparent_png(png)


def _recover_download_file(own_folder, user_folder, snapshots, observed, destination, acknowledged=False):
    """备用下载回收：优先隔离的浏览器目录；用户下载目录仅在网站确认成功时考虑。

    每个候选必须是本次点击后新增/修改、连续两次扫描文件大小稳定、PNG 完整透明。
    在用户「下载」目录同时出现多个候选时不猜测归属，防止串图。
    """
    for folder, source in ((own_folder, '浏览器临时下载'), (user_folder, '系统下载目录')):
        if folder is None or (source == '系统下载目录' and not acknowledged) or not folder.is_dir():
            continue
        before = snapshots.get(str(folder), {})
        accepted = []
        try:
            for f in folder.iterdir():
                if not f.is_file() or f.is_symlink() or f.suffix.lower() in ('.crdownload', '.download', '.part', '.tmp'):
                    continue
                stat = f.stat()
                ident = (stat.st_mtime_ns, stat.st_size)
                if ident == before.get(f.name) or not (45 <= stat.st_size <= 65 * 1024 * 1024):
                    continue
                key = str(f)
                if observed.get(key) != ident:
                    observed[key] = ident
                    continue
                try:
                    data = _read_download_image(f.read_bytes())
                    accepted.append((f, data))
                except Exception:
                    # 下载可能仍在写入，或 ZIP 已损坏；不让坏文件中断整个任务队列。
                    continue
        except OSError:
            continue
        if len(accepted) == 1:
            path, image_bytes = accepted[0]
            destination = Path(destination)
            part = destination.with_suffix('.verified.part')
            part.write_bytes(image_bytes)
            part.replace(destination)
            print('[下载补救] 已确认完整 PNG：', source, path.name, flush=True)
            return True
        if len(accepted) > 1:
            print('[下载补救] 新文件不止一张，跳过自动认领，防止贴错产品', source, flush=True)
    return False


def _visible_text(page, pattern):
    try:
        found=page.get_by_text(re.compile(pattern,re.I))
        for i in range(min(found.count(),10)):
            if found.nth(i).is_visible(timeout=130): return found.nth(i)
    except Exception: pass
    return None


def _check_remember_option(page, pattern):
    """只对包含精确文案的 label/input 操作，避免触及推荐卡片。"""
    try:
        labels=page.locator('label').filter(has_text=re.compile(pattern,re.I))
        for i in range(min(labels.count(),12)):
            label=labels.nth(i)
            if not label.is_visible(timeout=120): continue
            box=label.locator('input[type="checkbox"]')
            if box.count():
                if not box.first.is_checked(): box.first.check(force=True,timeout=1400)
                return True
            # 自定义开关由 label 自身响应点击
            label.click(timeout=1200)
            return True
        # 自定义弹窗可能 checkbox 和文字不在同一个 label 中
        t=_visible_text(page,pattern)
        if t is not None:
            parent=t.locator('xpath=..')
            for _ in range(3):
                box=parent.locator('input[type="checkbox"]')
                if box.count():
                    if not box.first.is_checked():box.first.check(force=True,timeout=1500)
                    return True
                parent=parent.locator('xpath=..')
    except Exception as exc:
        print('[记住选项]',str(exc)[:140],flush=True)
    return False


def choose_png_format(page):
    """图六：同时看到下载 PNG/JPG 才认为格式选择弹窗出现。"""
    if page.is_closed(): return False
    try:
        # Current site also has PNG/JPG cards and separate SD/HD buttons.
        hd = page.get_by_role('button', name=re.compile(r'高清下载', re.I))
        sd = _visible_text(page, r'^\s*标清下载\s*$')
        if sd is not None and _visible_text(page, r'^\s*选择下载格式\s*$') is not None:
            png = _visible_text(page, r'^\s*PNG\s*$')
            if png is not None:
                card = png.locator('xpath=..')
                for _ in range(3):
                    if card.locator('text="透明背景"').count():
                        card.click(timeout=2500)
                        break
                    card = card.locator('xpath=..')
                for i in range(min(hd.count(), 4)):
                    if hd.nth(i).is_visible(timeout=150):
                        _check_remember_option(page,r'记住.*(?:格式|选择)')
                        hd.nth(i).click(timeout=3500)
                        return True
        png_buttons=page.get_by_role('button',name=re.compile(r'^\s*下载\s*PNG\s*$',re.I))
        jpg_present=_visible_text(page,r'^\s*(?:下载\s*)?JPG\s*$') is not None
        png_btn=next((png_buttons.nth(i) for i in range(min(png_buttons.count(),8))
                      if png_buttons.nth(i).is_visible(timeout=120)),None)
        if png_btn is None:
            maybe=page.get_by_text(re.compile(r'^\s*下载\s*PNG\s*$',re.I))
            png_btn=next((maybe.nth(i) for i in range(min(maybe.count(),6)) if maybe.nth(i).is_visible(timeout=120)),None)
        if png_btn is None or not jpg_present:
            return False
        _check_remember_option(page,r'记住.*(?:格式|选择)|下次.*(?:选择|格式)')
        png_btn.click(timeout=3500)
        print('[格式弹窗] 已选择下载 PNG',flush=True)
        return True
    except Exception as exc:
        print('[格式弹窗处理]',str(exc)[:180],flush=True)
        return False


def dismiss_download_success(page):
    """图七：勾选 24 小时不推荐并关闭弹窗，绝不关闭整个任务页。"""
    if page.is_closed() or not site_download_acknowledged(page): return False
    _check_remember_option(page,r'24\s*小时.*不再显示|不再显示推荐')
    selectors=[
        '[role="dialog"] button[aria-label*="关闭"]',
        '[role="dialog"] button[aria-label*="Close"]',
        '.ant-modal-close', '.el-dialog__headerbtn',
        '[class*="modal"] [class*="close"]',
        '[class*="dialog"] [class*="close"]',
    ]
    for sel in selectors:
        try:
            items=page.locator(sel)
            for i in range(min(items.count(),8)):
                elt=items.nth(i)
                if elt.is_visible(timeout=100):
                    elt.click(timeout=1500)
                    if not site_download_acknowledged(page):
                        print('[下载弹窗] 已关闭推荐弹窗，保留原任务页',flush=True)
                        return True
        except Exception: pass
    # 某些网站关闭按钮是无文字的 x/SVG，按弹窗标题最近的顶层容器找右上角关闭控件。
    try:
        clicked=page.evaluate(r"""() => {
          const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'};
          const heads=[...document.querySelectorAll('h1,h2,h3,h4,div,span,strong,b')]
            .filter(e=>visible(e)&&e.children.length<3&&/^下载成功[!！]?$/.test((e.innerText||'').trim()));
          for(const head of heads){let cur=head;
            for(let n=0;n<9&&cur;n++,cur=cur.parentElement){
              const r=cur.getBoundingClientRect(),t=(cur.innerText||'');
              if(r.width<290||r.width>Math.min(window.innerWidth*.98,900)||r.height<140||r.height>window.innerHeight*.95)continue;
              if(!/24.?小时|不再显示推荐/.test(t))continue;
              const nodes=[...cur.querySelectorAll('button,[role="button"],a,[class*="close"],svg')];
              const opts=nodes.filter(e=>visible(e)).map(e=>{const b=e.getBoundingClientRect(),s=(e.innerText||'')+' '+(e.getAttribute('aria-label')||'')+' '+(e.className?.baseVal||e.className||'');return {e,b,s}})
                .filter(v=>v.b.right>r.right-85&&v.b.top<r.top+90&&v.b.width<80&&v.b.height<80)
                .sort((a,b)=>a.b.top-b.b.top);
              const chosen=opts.find(v=>/close|关闭|×|✕|cross|cancel/i.test(v.s))||opts[0];
              if(chosen){chosen.e.click();return true;}
            }
          }return false;
        }""")
        if clicked:
            print('[下载弹窗] 已点右上角关闭，任务页面继续复用',flush=True)
            return True
    except Exception as exc: print('[下载弹窗关闭]',str(exc)[:160],flush=True)
    print('[下载弹窗] 未找到安全关闭按钮；保留当前页面，由下一次上传前再次处理',flush=True)
    return False


def try_download(page, output_path, job):
    """Install listeners BEFORE any format/download control can produce a file."""
    context = page.context
    if not getattr(context, '_patchi_capture_installed', False):
        context.add_init_script(DOWNLOAD_CAPTURE_JS)
        context._patchi_capture_installed = True
    page.evaluate(DOWNLOAD_CAPTURE_JS)
    page.evaluate('() => window.__patchiDownloadCapture.begin()')
    captured, watched = [], []
    def on_download(item):
        captured.append(item)
    def watch(pg):
        if pg not in watched:
            watched.append(pg)
            pg.on('download', on_download)
    for pg in context.pages:
        watch(pg)
    context.on('page', watch)
    own_dir = getattr(context, '_patchi_download_dir', None)
    snapshots = {str(p): _snapshot_downloads(p) for p in (own_dir, DOWNLOAD_DIR) if p is not None}
    guard = getattr(page, '_patchi_download_guard', None)
    if guard is not None:
        guard['until'] = time.monotonic() + 60
    try:
        return _try_download_listening(page, output_path, job, captured, own_dir, snapshots)
    finally:
        try: page.evaluate('() => window.__patchiDownloadCapture?.end()')
        except Exception: pass
        if guard is not None:
            guard['until'] = 0
        try: context.remove_listener('page', watch)
        except Exception: pass
        for pg in watched:
            try: pg.remove_listener('download', on_download)
            except Exception: pass


def _try_download_listening(page, output_path, job, captured, own_dir, snapshots):
    """下载事件 + 实体文件兜底；成功弹窗之后不再盲目重复点击。"""
    assert_on_site(page)
    check_login(page)
    candidates, info = [], {}
    for tick in range(14):
        # 二级格式弹窗若在重试/等待后残留，先处理它，不重新点击上一层按钮。
        if choose_png_format(page):
            page.wait_for_timeout(300)
            if captured:
                for event in list(captured):
                    extract_download(event, output_path)
                    return True
        if recover_anchor_download(page, output_path):
            return True
        candidates, info = probe_download_candidates(page)
        if candidates:
            break
        if result_visible(page):
            legacy = list(visible_controls(page, DOWNLOAD_LABEL))
            if legacy:
                candidates = [(x, {'label': '下载高清大图', 'score': 90}) for x in legacy[:6]]
                break
        scroll_for_controls(page, tick)
        page.wait_for_timeout(650)
    if not candidates:
        raise RuntimeError('未定位结果区下载按钮；DOM扫描：' + json.dumps(info, ensure_ascii=False)[:280])
    print('[下载定位]', json.dumps(info.get('candidates', [])[:5], ensure_ascii=False)[:850], flush=True)

    context = page.context
    observed = {}
    rendered_checked = False

    def save_captured():
        while captured:
            event = captured.pop(0)
            try:
                failure = event.failure()  # waits until browser finishes writing bytes
                if failure:
                    print('[下载事件] 下载失败:', failure, flush=True)
                    continue
                extract_download(event, output_path)
                has_alpha_png(output_path)
                set_job(job, 'downloaded', '已验证 PNG，保存至：' + output_path.name)
                return True
            except Exception as exc:
                print('[下载事件] 下载文件未通过验证:', str(exc)[:220], flush=True)
        return False

    def recover(confirmed=False):
        nonlocal rendered_checked
        if not page.is_closed() and recover_anchor_download(page, output_path):
            set_job(job, 'downloaded', '已验证网站实际下载的透明 PNG')
            return True
        if _recover_download_file(own_dir, DOWNLOAD_DIR, snapshots, observed,
                                  output_path, acknowledged=confirmed):
            has_alpha_png(output_path)
            set_job(job, 'downloaded', '已从真实下载文件恢复并验证 PNG：' + output_path.name)
            return True
        if confirmed and not rendered_checked and not page.is_closed():
            rendered_checked = True
            if recover_rendered_png(page, output_path, job):
                set_job(job, 'downloaded', '已从网站当前结果恢复高清透明 PNG')
                return True
        return False

    def wait_and_confirm(timeout=32):
        acknowledged = False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with lock:
                if stop_flag:
                    raise RuntimeError('用户已停止本批次任务')
            if save_captured():
                return True, acknowledged
            if not is_browser_context_alive(context):
                # 浏览器退出前可能已经成功写出文件，先检查下载目录。
                if recover(acknowledged):
                    return True, acknowledged
                raise BrowserSessionLost('下载中浏览器意外退出，尚未找到完整 PNG')
            alive = not page.is_closed()
            if alive:
                # 图六的“下载 PNG”二级确认也在同一次下载监听窗口内点击。
                choose_png_format(page)
                acknowledged = acknowledged or site_download_acknowledged(page)
                if not acknowledged:
                    reason = login_detected(page)
                    if reason:
                        raise LoginRequired('网站下载时要求登录：' + reason)
            if acknowledged:
                set_job(job, 'running', '网站显示下载成功，正在核实透明 PNG 是否已保存到电脑')
            if recover(acknowledged):
                return True, acknowledged
            pulse = page if alive else next((p for p in context.pages if not p.is_closed()),None)
            if pulse is None: raise BrowserSessionLost('工作标签已关闭，没有可用下载确认页')
            try:
                pulse.wait_for_timeout(280)
            except Exception as exc:
                if not is_browser_context_alive(context):
                    if recover(acknowledged):
                        return True, acknowledged
                    raise BrowserSessionLost('浏览器在文件落盘前断开') from exc
                if page.is_closed():
                    continue
                raise
        # 最终检查两次，给尚在写盘的文件完成一次状态稳定性观测。
        if save_captured() or recover(acknowledged):
            return True, acknowledged
        return False, acknowledged

    try:
        errors = []
        for locator, meta in candidates[:2]:
            for method in ('normal', 'dom'):
                if page.is_closed():
                    raise RuntimeError('下载按钮所在的任务页面已关闭，必须重建任务页')
                if site_download_acknowledged(page):
                    # 弹窗挡住了按钮时，不能尝试坐标点击其他业务/广告区域。
                    done, _ = wait_and_confirm(timeout=DOWNLOAD_DIALOG_TIMEOUT)
                    if done:
                        try: dismiss_download_success(page)
                        except Exception: pass
                        return True
                    raise DownloadAcknowledgedWithoutFile('网站已显示下载成功，但助手未找到完整透明 PNG。请检查系统下载位置或下载权限。')
                assert_on_site(page)
                set_job(job, 'running', '尝试下载：' + meta.get('label', '下载高清大图') + '（' + method + '）')
                click_error = None
                guard = getattr(page, '_patchi_download_guard', None)
                if guard is not None:
                    guard['until'] = time.monotonic() + 60
                try:
                    locator.scroll_into_view_if_needed(timeout=4000)
                    if method == 'normal':
                        locator.click(timeout=6500)
                    elif method == 'mouse':
                        rect = locator.bounding_box(timeout=2500)
                        if not rect:
                            raise RuntimeError('无法获得下载按钮位置')
                        page.mouse.move(rect['x'] + rect['width'] / 2, rect['y'] + rect['height'] / 2)
                        page.mouse.click(rect['x'] + rect['width'] / 2, rect['y'] + rect['height'] / 2, delay=75)
                    else:
                        locator.evaluate('(element)=>element.click()')
                except Exception as exc:
                    click_error = exc
                if click_error is not None:
                    print('[下载点击诊断]', str(click_error)[:180], flush=True)
                # 点击可能成功后立即关闭触发页，因此始终等待/回收，而不因为 click() 抛错直接重复点。
                done, acknowledged = wait_and_confirm(timeout=DOWNLOAD_CONFIRM_TIMEOUT if click_error is None else DOWNLOAD_CLICK_ERROR_TIMEOUT)
                if done:
                    try: page.wait_for_timeout(550); dismiss_download_success(page)
                    except Exception: pass
                    return True
                if acknowledged:
                    raise DownloadAcknowledgedWithoutFile(
                        '网站弹出「下载成功」但浏览器没有可确认的透明 PNG 文件；停止重复点击，防止误触广告。'
                        '请确认浏览器下载权限与网站真实下载目录。')
                if page.is_closed():
                    raise RuntimeError('任务页面已关闭且尚无有效 PNG，将新开任务页重试')
                if click_error is not None and is_browser_disconnected(click_error):
                    if not is_browser_context_alive(context):
                        raise BrowserSessionLost('下载动作中浏览器连接失效') from click_error
                errors.append(method + '：' + (str(click_error)[:170] if click_error else '未产生文件下载'))
                check_login(page)
        raise RuntimeError('点击结果下载按钮后仍未取得 PNG：' + ' | '.join(errors[-4:]))
    finally:
        pass  # The outer listener scope also covers discovery and PNG-format clicks.


def recover_rendered_png(page, destination, job):
    """Fallback to the actual displayed result, after the website acknowledges download.

    Reads only rendered image/link URLs from the current page. Does not discover
    API endpoints, guess remote files, or accept a lower-resolution preview.
    """
    import io
    import hashlib
    from PIL import Image
    try:
        with Image.open(job['source']) as image:
            required = image.size
        candidates = page.evaluate(r"""async () => {
          const visible = e => {const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>1&&r.height>1&&s.display!=='none'&&s.visibility!=='hidden'};
          const urls = new Set();
          for(const a of document.querySelectorAll('a[download][href]')) {
            if(visible(a)&&!a.closest('header,footer,nav,aside')) urls.add(a.href);
          }
          for(const im of document.images) {
            const r=im.getBoundingClientRect();
            if(!visible(im)||im.naturalWidth<140||im.naturalHeight<140||im.closest('header,footer,nav,aside'))continue;
            if(im.closest('[class*="result" i],[class*="output" i],[data-result]') || r.x>innerWidth*.37) urls.add(im.currentSrc||im.src);
          }
          const files=[];
          for(const url of [...urls].slice(0,8)) {
            if(!/^(https:\/\/|blob:|data:image\/)/i.test(url))continue;
            const ctl=new AbortController(),timer=setTimeout(()=>ctl.abort(),4000);
            try {
              const response=await fetch(url,{signal:ctl.signal});
              if(!response.ok)continue;
              const blob=await response.blob();
              if(blob.size>65*1024*1024)continue;
              const data=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result);r.onerror=reject;r.readAsDataURL(blob)});
              files.push(data);
            } catch(_) {} finally {clearTimeout(timer)}
          }
          return files;
        }""")
        valid = {}
        for data in candidates:
            try:
                raw = validate_transparent_png(base64.b64decode(data.split(',', 1)[1], validate=True))
                with Image.open(io.BytesIO(raw)) as image:
                    if image.width < required[0] or image.height < required[1]: continue
                    rgba = image.convert('RGBA')
                    digest = hashlib.sha256(rgba.tobytes()).hexdigest()
                valid[digest] = raw
            except Exception:
                continue
        if len(valid) != 1:
            return False
        part = Path(destination).with_suffix('.verified.part')
        part.write_bytes(next(iter(valid.values())))
        part.replace(destination)
        return True
    except Exception as exc:
        print('[页面结果回收]', str(exc)[:120], flush=True)
        return False


def recover_anchor_download(page, destination):
    """Recover the site's actual download payload; exclude blobs from older tasks."""
    try:
        values = page.evaluate('() => window.__patchiDownloadCapture?.take() || []')
        valid = {}
        import hashlib
        for value in values:
            try:
                raw = _read_download_image(base64.b64decode(value.split(',', 1)[1], validate=True))
                valid[hashlib.sha256(raw).hexdigest()] = raw
            except Exception:
                continue
        if len(valid) != 1: return False
        part = Path(destination).with_suffix('.verified.part')
        part.write_bytes(next(iter(valid.values())))
        part.replace(destination)
        return True
    except Exception:
        return False


def save_failure_capture(page, job):
    try:
        if page.is_closed():
            return
        path = Path(workdir.name) / (job['id'] + '_error.png')
        page.screenshot(path=str(path), full_page=True, timeout=7000)
        job['diagnostics'] = str(path)
        debug_path = Path(workdir.name) / (job['id'] + '_debug.txt')
        body_text = page.locator('body').inner_text(timeout=2200)[:8500]
        controls = page.locator('button, [role="button"], a[download]')
        summary = controls.evaluate_all("""els => els.slice(0, 160).map(e => ({
            label: (e.innerText || e.getAttribute('aria-label') || '').slice(0, 150),
            title: e.getAttribute('title') || '',
            className: String(e.className || '').slice(0, 120),
            visible: !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)
        }))""")
        candidate_debug=page.evaluate(DOWNLOAD_PROBE_JS)
        debug_path.write_text('当前页面：' + page.url + '\n\n智能定位候选：\n' + json.dumps(candidate_debug,ensure_ascii=False,indent=2) + '\n\n页面文字：\n' + body_text +
                              '\n\n疑似按钮：\n' + json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        job['debug'] = str(debug_path)
    except Exception as e:
        print('[诊断采集]', str(e)[:140], flush=True)


def cleanup_context_tabs(context, keep_page=None):
    """正常处理期间不关闭任何页面；下载过程中创建的临时新标签交由浏览器管理。"""
    return


def prepare_work_page(context, page=None):
    if not is_browser_context_alive(context):
        raise BrowserSessionLost('浏览器连接已断开')
    if page is None or page.is_closed():
        # persistent_context 启动时已自带初始标签，不再另开一个 about:blank 窗口/守护标签。
        page=next((p for p in context.pages if not p.is_closed()),None)
        if page is None: page=context.new_page()
        install_navigation_guard(context,page)
    return page


def reset_work_page(page, status_text='正在打开抠图网站'):
    try:
        page.goto(SITE, wait_until='domcontentloaded', timeout=60000)
    except Exception:
        try:
            page.goto(SITE, wait_until='load', timeout=70000)
        except Exception:
            raise
    assert_on_site(page)
    page.wait_for_timeout(900)
    check_login(page)
    return page


def run_automation():
    """一个持久后台标签：依次上传→开始→等待→下载 PNG→关闭推荐弹窗→下一张。"""
    global busy, finished, stop_flag, message
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        with lock:
            message='请安装 Playwright，并准备较新版本的桌面版 Microsoft Edge 或 Google Chrome'
            for j in queue:
                if j['status']=='ready': set_job(j,'failed',message)
            busy,finished=False,True
        return
    context=None
    page=None
    try:
        with sync_playwright() as playwright:
            context=restart_saved_browser(playwright)
            page=prepare_work_page(context)
            # 只有首次启动时导航到目标网站。后续任务不重新开标签、不主动刷新网页。
            reset_work_page(page)
            blocked=False
            total=len(queue)
            for index,job in enumerate(list(queue),1):
                with lock:
                    if stop_flag or blocked:
                        set_job(job,'stopped','网站需要人工登录' if blocked else '用户停止')
                        continue
                    message=f'抠图 {index}/{total}：{job["name"]}'
                result_path=unique_download_path(job,index)
                for attempt in range(1,4):
                    try:
                        with lock:
                            if stop_flag: raise RuntimeError('用户停止任务')
                        if not is_browser_context_alive(context):
                            context=restart_saved_browser(playwright,context)
                            page=None
                        page=prepare_work_page(context,page)
                        # 只在同页上一任务失败/浏览器重建时做导航复位。
                        if attempt>1 or not safe_site_url(page.url):
                            reset_work_page(page)
                        else:
                            dismiss_download_success(page)
                        set_job(job,'running',f'{index}/{total}：上传第 {index} 张（同一页面）')
                        had_previous_result=index>1 and attempt==1 and result_visible(page)
                        upload_to_site(page,job['source'])
                        # 网站可能保留上一张图的结果；优先点击开始按钮，绝不能把上一张的完成提示误认成新任务。
                        started=start_processing(page,job)
                        if had_previous_result and started=='auto':
                            set_job(job,'running','检测到上张结果仍留在页面；仅刷新当前标签重新上传，避免错图')
                            reset_work_page(page)
                            upload_to_site(page,job['source'])
                            start_processing(page,job)
                        wait_for_result(page,job)
                        if not try_download(page,result_path,job):
                            raise RuntimeError('下载没有生成可确认的 PNG')
                        has_alpha_png(result_path)
                        job['result']=str(result_path)
                        set_job(job,'done','已下载透明 PNG；原页面继续处理下一张，最终统一回填')
                        print(f'[完成 {index}/{total}] {job["name"]} -> {result_path}',flush=True)
                        try: dismiss_download_success(page)
                        except Exception: pass
                        break
                    except Exception as exc:
                        detail=str(exc)[:350]
                        print(f'[异常 {index}/{total} 第{attempt}/3次] {detail}',flush=True)
                        if page is not None: save_failure_capture(page,job)
                        if result_path.exists():
                            try:
                                has_alpha_png(result_path)
                                job['result']=str(result_path)
                                set_job(job,'done','异常后找回已下载 PNG，继续下一张')
                                break
                            except Exception: pass
                        if isinstance(exc,LoginRequired): blocked=True
                        with lock: stopping=stop_flag
                        if not stopping and not blocked and (context is None or not is_browser_context_alive(context)):
                            try:
                                context=restart_saved_browser(playwright,context)
                                page=None
                            except Exception as recovery:
                                context=None
                                detail+='；恢复失败：'+str(recovery)[:140]
                        if attempt>=3 or stopping or blocked:
                            set_job(job,'stopped' if stopping or blocked else 'failed',detail)
                        else:
                            set_job(job,'running','当前页面复位后重试，保留登录数据：'+detail[:90])
                            time.sleep(.7)
                with lock:
                    count=sum(j['status']=='done' for j in queue)
                    message=f'已处理 {index}/{total} 张，成功下载 {count} 张；继续下一张'
    except Exception as exc:
        print('[自动化中断]',traceback.format_exc()[-1900:],flush=True)
        with lock:
            message='本批次中断：'+str(exc)[:250]
            for j in queue:
                if j['status'] in ('ready','running'):set_job(j,'failed',message)
    finally:
        # 仅整批任务完成之后退出自动化会话，不清理 Cookies、历史记录或系统下载文件。
        if context is not None:
            try: context.close()
            except Exception: pass
        with lock:
            busy,finished=False,True
            done=sum(j['status']=='done' for j in queue)
            message=f'已处理完成：{done}/{len(queue)} 张成功下载，可统一回填；失败项保留原图。'


# --- Gemini AI size-table recognition: Key never leaves localhost except to Google via HTTPS. ---
GEMINI_MODELS = (
    'gemini-3.8-flash',       # complex multi-column size tables and low-contrast assets
    'gemini-3.7-flash',       # complicated layouts / accurate secondary choice
    'gemini-3.6-flash',       # common product metadata and size tables
    'gemini-3.5-flash',       # structured data from dense screenshots
    'gemini-3.5-flash-lite',  # faster small simple product images
    'gemini-3.1-flash-lite',  # lightweight legacy fallback
    'gemini-2.5-flash',       # legacy project compatibility
    'gemini-2.5-flash-lite',  # legacy inexpensive fallback
)
GEMINI_API_PROTOCOL = 6  # expanded model choices and persistent selection
GEMINI_DIR = (Path(os.environ.get('LOCALAPPDATA', str(Path.home()/'.local'/'share'))) / '帕琪工坊')
GEMINI_SECRET = GEMINI_DIR / 'gemini_api_key.dpapi'
GEMINI_CONFIG = GEMINI_DIR / 'gemini_settings.json'
gemini_lock = threading.RLock()
gemini_key = ''
gemini_model = GEMINI_MODELS[0]
gemini_saved = False


def dpapi_transform(data, protect=True):
    """Windows DPAPI user-bound encryption. Non-Windows does NOT store plaintext key."""
    if os.name != 'nt':
        raise RuntimeError('加密保存密钥仅支持 Windows；其他系统本次运行有效')
    import ctypes
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]
    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    crypt32.CryptProtectData.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt32.CryptProtectData.restype = crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    inp = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    out = Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(inp), '帕琪工坊 Gemini API', None, None, None, 1, ctypes.byref(out)):
        raise OSError(ctypes.get_last_error(), 'Windows DPAPI 操作失败')
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def load_gemini_local():
    global gemini_key, gemini_model, gemini_saved
    try:
        config = json.loads(GEMINI_CONFIG.read_text('utf-8'))
        if config.get('model') in GEMINI_MODELS: gemini_model = config['model']
    except (OSError, ValueError): pass
    try:
        if GEMINI_SECRET.exists() and os.name == 'nt':
            gemini_key = dpapi_transform(GEMINI_SECRET.read_bytes(), False).decode('utf-8')
            gemini_saved = True
    except Exception as exc:
        print('[Gemini] 密钥解密失败，请在编辑器重新输入：', str(exc)[:120], flush=True)


from patchi_ai import Pool, Jobs
from patchi_ai_prompts import *
ai_pool = Pool(GEMINI_DIR, dpapi_transform)
ai_jobs = Jobs()

def set_gemini_settings(data):
    return ai_pool.configure(data)

def public_gemini_settings():
    return ai_pool.public()

def available_gemini_models():
    return ai_pool.list_models()

# 商品资料字段保持开放；图片里的成分项目不可通过穷举字段名判断。
# 只过滤明确与商品规格展示无关的管理字段。
PRODUCT_META_ALIASES = {
    '产品名称':'品名','商品名称':'品名','商品类型':'品名',
    '色彩':'颜色','可选颜色':'颜色','大小':'尺码','号型':'尺码'
}
PRODUCT_NOISE_LABELS = frozenset((
    '货号','款号','商品货号','SKU','流水号','编号','货价','售价','单价',
    '报价','采购价','成本价','供应商','联系人','联系电话','网址',
    '二维码','备注（内部）','宣传语','库存数','订单数',
))


def normalize_product_result(raw):
    """Preserve the OCR text and order, including newlines and previously unknown material labels."""
    if not isinstance(raw, dict):
        raise ValueError('商品信息结果不是对象')
    fields = []
    for row in (raw.get('fields') or [])[:100]:
        if not isinstance(row, dict):
            continue
        label = str(row.get('label') or '').strip()[:80]
        label = PRODUCT_META_ALIASES.get(label, label)
        # Reformat ONLY line separators and outer whitespace; do not normalize away the material text.
        value = str(row.get('value') or '').replace('\r\n', '\n').replace('\r', '\n')
        value = value.replace('\u2028', '\n').replace('\u2029', '\n')
        value = '\n'.join(line.strip() for line in value.split('\n') if line.strip())[:4000]
        evidence = str(row.get('evidence') or '').strip()[:500] or value.split('\n')[0][:200]
        if not label or not value or label.upper() in PRODUCT_NOISE_LABELS or label in PRODUCT_NOISE_LABELS:
            continue
        if re.fullmatch(r'(?:无|暂无|未知|未注明|无信息|不确定|N/?A|--?|-|/)', value, re.I):
            continue
        if value.lower() in ('null','none','undefined'):
            continue
        if label == '品名' and re.fullmatch(r'[\dA-Za-z_-]+', value):
            continue
        # Do NOT deduplicate same-name material layers or sort fields: these operations lose source order.
        fields.append({'label': label, 'value': value, 'evidence': evidence})
    if not fields:
        raise ValueError('没有识别到清晰的服装文字信息，请检查图片清晰度')
    return {'fields': fields[:60], 'notes': [str(x)[:180] for x in (raw.get('notes') or [])[:12]]}


def normalize_gemini_result(raw):
    result = {}
    if not isinstance(raw, dict): raise ValueError('Gemini 返回内容不是表格对象')
    total = 0
    for typ in ('size', 'recommend'):
        table = raw.get(typ, {})
        if not isinstance(table, dict): table = {}
        columns = [str(c)[:90] for c in (table.get('columns') or [])[:16]]
        rows = []
        for row in (table.get('rows') or [])[:100]:
            if isinstance(row, list):rows.append([str(x if x is not None else '')[:90] for x in row[:16]])
        width = max([len(columns)] + [len(x) for x in rows])
        if width and not columns: columns = ['待核对：第%d列'%(i+1) for i in range(width)]
        columns += ['待核对：第%d列'%(i+1) for i in range(len(columns),width)]
        rows = [r + ['']*(width-len(r)) for r in rows]
        total += len(rows)
        result[typ] = {'columns': columns, 'rows': rows, 'uncertain': [str(x)[:130] for x in (table.get('uncertain') or [])[:30]]}
    result['notes'] = [str(x)[:200] for x in (raw.get('notes') or [])[:30]]
    if not total: raise ValueError('Gemini 未识别到有效尺码表；请确认图片清晰并包含表格')
    return result



def normalize_detail_caption(raw):
    if not isinstance(raw,dict):raise ValueError('细节图文案必须是 JSON 对象')
    caption=re.sub(r'[\r\n\t]+',' ',str(raw.get('caption') or '')).strip().strip('"“”')
    caption=re.sub(r'\s{2,}',' ',caption)
    if not caption or len(caption)>80:raise ValueError('文案为空或超出长度限制')
    if re.search(r'防水|防泼水|防雨|100%|保证|永久|绝对',caption):raise ValueError('文案出现未证实性能词，请重新生成')
    return caption[:48]

def gemini_recognize(data, product=False, detail=False):
    if not isinstance(data, dict): raise ValueError('请求格式错误')
    with gemini_lock:
        key, model = bool(ai_pool.apis), data.get('model') or ai_pool.model
    if not key: raise ValueError('请先在 Gemini API 设置中填写并保存 API Key')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}',model): raise ValueError('模型名称无效')
    mime = data.get('mimeType')
    if mime not in ('image/png', 'image/jpeg', 'image/webp'): raise ValueError('只支持 PNG/JPG/WebP 图片')
    encoded = data.get('image', '')
    if not isinstance(encoded, str) or len(encoded) > 17*1024*1024:
        raise ValueError('图片太大，建议使用不超过12MB的图片')
    try: raw_img = base64.b64decode(encoded, validate=True)
    except Exception: raise ValueError('图片数据损坏')
    if len(raw_img) > 12*1024*1024 or len(raw_img) < 32: raise ValueError('图片大小应为 32B ~ 12MB')
    if not ((mime=='image/png' and raw_img.startswith(b'\x89PNG\r\n\x1a\n')) or
            (mime=='image/jpeg' and raw_img.startswith(b'\xff\xd8\xff')) or
            (mime=='image/webp' and raw_img.startswith(b'RIFF') and raw_img[8:12]==b'WEBP')):
        raise ValueError('图片格式与内容不匹配')
    payload = {'contents': [{'role':'user', 'parts':[{'text':DETAIL_CAPTION_PROMPT if detail else PRODUCT_PROMPT if product else GEMINI_PROMPT},
        {'inline_data': {'mime_type':mime, 'data':encoded}}]}],
        'generationConfig': {'temperature':0, 'maxOutputTokens':8192,
            'responseMimeType':'application/json', 'responseSchema':DETAIL_CAPTION_SCHEMA if detail else PRODUCT_SCHEMA if product else TABLE_SCHEMA}}
    answer = ai_pool.generate(payload, model)
    try:
        candidates = answer.get('candidates') or []
        content = candidates[0]['content']['parts']
        text = ''.join(p.get('text','') for p in content)
        data = json.loads(text)
        table = normalize_detail_caption(data) if detail else normalize_product_result(data) if product else normalize_gemini_result(data)
    except (KeyError,IndexError,TypeError,json.JSONDecodeError,ValueError) as exc:
        raise RuntimeError('Gemini 没有返回有效的结构化数据：'+str(exc)[:170])
    return {'ok':True,'model':model,('caption' if detail else 'product' if product else 'tables'):table}


# 详情图套版定位：Gemini 只输出坐标；实际 PNG 像素合成在浏览器本地完成。



def normalize_garment_contour(answer):
    """Reject malformed or implausibly large shapes instead of accidentally erasing typography."""
    if not isinstance(answer,dict): raise ValueError('轮廓识别结果必须为对象')
    raw=answer.get('polygon') or []
    if not isinstance(raw,list) or not (3<=len(raw)<=90):raise ValueError('AI 返回的轮廓点数量异常')
    poly=[]
    for pt in raw:
        if not isinstance(pt,dict): raise ValueError('轮廓点格式错误')
        x,y=pt.get('x'),pt.get('y')
        if isinstance(x,bool) or isinstance(y,bool) or not isinstance(x,(int,float)) or not isinstance(y,(int,float)) or not (0<=x<=1000 and 0<=y<=1000):
            raise ValueError('轮廓坐标超出图片范围')
        pair=[int(round(x)),int(round(y))]
        if not poly or pair!=poly[-1]:poly.append(pair)
    if len(poly)>3 and poly[0]==poly[-1]:poly.pop()
    if len(poly)<3:raise ValueError('AI 未找到完整衣物轮廓')
    x0=min(p[0] for p in poly);x1=max(p[0] for p in poly)
    y0=min(p[1] for p in poly);y1=max(p[1] for p in poly)
    if (x1-x0)<15 or (y1-y0)<15 or (x1-x0)*(y1-y0)>820000:
        raise ValueError('衣物识别区域过小或过大，已阻止错误替换')
    conf=max(0.0,min(1.0,float(answer.get('confidence') or 0)))
    return {'ok':True,'polygon':poly,'category':str(answer.get('category') or 'garment')[:75], 'confidence':conf}


def gemini_garment_contour(request_data):
    with gemini_lock:
        key,model=bool(ai_pool.apis),request_data.get('model') or ai_pool.model
    if not key:raise ValueError('请先通过 Gemini API 设置保存密钥')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}',model):raise ValueError('模型名称无效')
    click=request_data.get('click') or []
    if not isinstance(click,list) or len(click)!=2 or any(type(v) not in (int,float) or not 0<=v<=1000 for v in click):
        raise ValueError('点击坐标错误')
    if request_data.get('mimeType')!='image/jpeg':raise ValueError('套图定位要求带红色点击点的 JPEG 预览')
    encoded=request_data.get('image')
    if not isinstance(encoded,str) or len(encoded)>16*1024*1024:raise ValueError('待识别图片过大')
    try: raw=base64.b64decode(encoded,validate=True)
    except Exception:raise ValueError('图片编码无效')
    if not raw.startswith(b'\xff\xd8\xff'):raise ValueError('JPEG 图像无效')
    prompt=GARMENT_CONTOUR_PROMPT.format(x=round(click[0]),y=round(click[1]))
    payload={'contents':[{'role':'user','parts':[{'text':prompt},{'inline_data':{'mime_type':'image/jpeg','data':encoded}}]}],
             'generationConfig':{'temperature':0,'maxOutputTokens':5000,'responseMimeType':'application/json','responseSchema':GARMENT_CONTOUR_SCHEMA}}
    response=ai_pool.generate(payload,model)
    try:
        items=response['candidates'][0]['content']['parts']
        parsed=json.loads(''.join(x.get('text','') for x in items))
    except Exception as exc:raise RuntimeError('Gemini 没有返回有效轮廓：'+str(exc)[:150])
    result=normalize_garment_contour(parsed)
    return {**result,'model':model}


load_gemini_local()
if gemini_key and not ai_pool.apis:
    ai_pool.configure({'key':gemini_key,'model':gemini_model,'remember':gemini_saved,'name':'原有 API'})

def dispatch_ai(feature,payload,model):
    from patchi_mainphoto import process, process_overlay
    if feature=='main-image': return process(payload,True,model,lambda body:ai_pool.generate(body,model))
    if feature=='detail-current': return process_overlay(payload,True,model,lambda body:ai_pool.generate(body,model))
    raise ValueError('未知 AI 任务')

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print('%s %s' % (self.address_string(), self.path[:100]))

    def cors(self):
        origin = self.headers.get('Origin', '')
        if origin == 'null' or origin.startswith(('http://localhost:', 'http://127.0.0.1:')):
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Vary', 'Origin')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS, GET')
        self.send_header('Access-Control-Allow-Headers', 'X-API-Key, Content-Type, X-Patchi-Session')

    def reply(self, status, data, content_type):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control','no-store')
        self.cors(); self.end_headers()
        try: self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError): pass

    def reply_json(self, status, data):
        self.reply(status, json.dumps(data, ensure_ascii=False).encode('utf-8'), 'application/json; charset=utf-8')

    def do_OPTIONS(self):
        self.send_response(204); self.cors(); self.end_headers()

    def do_GET(self):
        if self.headers.get('Host', '').split(':')[0] not in ('127.0.0.1','localhost'):
            return self.reply_json(403, {'error':'拒绝非本机 Host'})
        if self.path.startswith('/ai/jobs/'):
            try: return self.reply_json(200, ai_jobs.get(self.path.rsplit('/',1)[-1]))
            except ValueError as exc:return self.reply_json(404,{'error':str(exc)})
        if self.path == '/gemini/settings':
            return self.reply_json(200, public_gemini_settings())
        if self.path == '/gemini/models':
            try: return self.reply_json(200, available_gemini_models())
            except ValueError as exc: return self.reply_json(400, {'error':str(exc)})
            except Exception as exc: return self.reply_json(502, {'error':str(exc)})
        if self.path in ('/','/index.html','/帕琪工坊_智能详情页制作工具.html'):
            path=Path(__file__).resolve().parent/'帕琪工坊_智能详情页制作工具.html'
            if not path.exists():return self.reply_json(404,{'error':'编辑器HTML不存在，请重新解压完整包'})
            return self.reply(200,path.read_bytes(),'text/html; charset=utf-8')
        if self.path == '/health':
            return self.reply_json(200, {'ok': True, 'geminiRecognition':True, 'productInfoRecognition':True, 'detailCaptionGeneration':True, 'garmentContourRecognition':True, 'samePhotoReplace':True, 'samePhotoReplaceProtocol':3, 'fastBatchReplace':True, 'crossPoseLogo':False, 'mainImageStudio':True, 'officialBgApi':False, 'samePhotoEngine':'3.4-balanced-detail', 'appVersion':'2026.10.16.11', 'workspaceSession':ai_jobs.session, 'blobDownloadRecovery':True, 'geminiApiProtocol':GEMINI_API_PROTOCOL, 'service':'patchi-helper', 'browserAutomation':True, 'automationProtocol':AUTOMATION_PROTOCOL, 'targetSite':SITE, 'navigationGuard':True, 'clickStartSupported':True, 'hdDownloadSupported':True, 'singleImageMode':True, 'downloadFolder':str(DOWNLOAD_DIR), 'autoOpenEditor':True, 'persistentLogin':True, 'loginActive':login_active, 'loginMessage':login_message, 'systemProfilePreferred': False, 'reuseSingleWindow': True, 'backgroundSinglePage': True, 'formatDialogPNG': True, 'recommendationDialogDismiss': True})
        if self.path == '/auto/status':
            return self.reply_json(200, api_state())
        if self.path.startswith('/auto/diagnostic/'):
            jid=unquote(self.path[len('/auto/diagnostic/'):])
            with lock:
                j=next((x for x in queue if x['id']==jid),None)
            if not j or not j.get('diagnostics'):
                return self.reply_json(404,{'error':'暂无错误截图'})
            return self.reply(200,Path(j['diagnostics']).read_bytes(),'image/png')
        if self.path.startswith('/auto/debug/'):
            jid=unquote(self.path[len('/auto/debug/'):])
            with lock:
                j=next((x for x in queue if x['id']==jid),None)
            if not j or not j.get('debug'):
                return self.reply_json(404,{'error':'暂无按钮诊断信息'})
            return self.reply(200,Path(j['debug']).read_bytes(),'text/plain; charset=utf-8')
        if self.path.startswith('/auto/result/'):
            jid = unquote(self.path[len('/auto/result/'):])
            with lock:
                j = next((x for x in queue if x['id']==jid and x['status']=='done'), None)
            if not j or not j.get('result'):
                return self.reply_json(404, {'error':'result not found'})
            try: return self.reply(200, has_alpha_png(j['result']), 'image/png')
            except Exception as e: return self.reply_json(500, {'error':str(e)})
        return self.reply_json(404, {'error':'not found'})

    def body(self, limit=LIMIT * 2):
        size = int(self.headers.get('Content-Length') or 0)
        if size <= 0 or size > limit: raise ValueError('无效或过大的请求')
        return self.rfile.read(size)

    def do_POST(self):
        global busy, finished, stop_flag, message, login_active, login_message
        try:
            if self.headers.get('Host', '').split(':')[0] not in ('127.0.0.1','localhost'):
                return self.reply_json(403, {'error':'拒绝非本机 Host'})
            origin=self.headers.get('Origin','')
            if origin and origin not in ('null',f'http://{HOST}:{PORT}',f'http://localhost:{PORT}'):
                return self.reply_json(403,{'error':'拒绝非本地编辑器请求'})
            if self.path.startswith(('/gemini/','/ai/')) and origin != f'http://{HOST}:{PORT}' and origin != f'http://localhost:{PORT}':
                return self.reply_json(403, {'error':'请从本机助手 http://127.0.0.1:8765 打开编辑器；不要双击 HTML 文件'})
            if self.path.startswith('/replace/batch/'):
                if origin not in (f'http://{HOST}:{PORT}', f'http://localhost:{PORT}'):
                    return self.reply_json(403, {'error':'请从本机编辑器启动批量套图'})
                from patchi_batch import handle as batch_handle
                return self.reply_json(200, batch_handle(self.path.rsplit('/',1)[-1],json.loads(self.body(limit=38*1024*1024))))
            if self.path == '/replace/analyze':
                # 本地私有服务；只从本机编辑器接受请求。
                if origin not in (f'http://{HOST}:{PORT}', f'http://localhost:{PORT}'):
                    return self.reply_json(403, {'error':'请从本地助手地址打开 HTML，不要直接双击 HTML'})
                try:
                    from patchi_samephoto import handle_payload
                except ImportError as exc:
                    return self.reply_json(503, {'error':'本地配准引擎未安装：请运行「打开帕琪工坊.bat」安装 numpy、Pillow、opencv-python。'+str(exc)[:120]})
                return self.reply_json(200, handle_payload(json.loads(self.body(limit=74*1024*1024))))
            if self.path == '/workspace/new-session':
                if origin or self.headers.get('X-Patchi-Launcher')!='1':
                    return self.reply_json(403,{'error':'仅启动器可开始新工作'})
                stop_flag=True
                return self.reply_json(200,{'session':ai_jobs.reset()})
            if self.path == '/ai/jobs':
                return self.reply_json(200,ai_jobs.submit(json.loads(self.body(limit=384*1024*1024)),dispatch_ai,ai_pool.model))
            if self.path == '/gemini/main-image':
                return self.reply_json(200,dispatch_ai('main-image',json.loads(self.body(limit=384*1024*1024)),ai_pool.model))
            if self.path == '/gemini/models/check':
                self.body(limit=4096)
                return self.reply_json(200, ai_pool.start_check())
            if self.path == '/gemini/settings':
                return self.reply_json(200, set_gemini_settings(json.loads(self.body(limit=65536))))
            if self.path == '/gemini/garment-contour':
                return self.reply_json(200, gemini_garment_contour(json.loads(self.body(limit=18*1024*1024))))
            if self.path in ('/gemini/recognize','/gemini/product','/gemini/detail-caption'):
                result=gemini_recognize(json.loads(self.body(limit=18*1024*1024)), product=self.path=='/gemini/product', detail=self.path=='/gemini/detail-caption')
                return self.reply_json(200, result)
            if self.path == '/auto/login/start':
                with lock:
                    if busy:
                        return self.reply_json(409,{'error':'请先等当前抠图任务结束，再打开登录窗口'})
                    if login_active:
                        return self.reply_json(200,{'ok':True,'message':login_message})
                    login_active=True
                    login_finish.clear()
                    login_message='正在打开独立浏览器，请稍候'
                threading.Thread(target=login_browser_worker,daemon=True).start()
                return self.reply_json(200,{'ok':True,'message':'浏览器将打开；登录后请点「完成登录并保存」'})
            if self.path == '/auto/login/finish':
                with lock:
                    if not login_active:
                        return self.reply_json(409,{'error':'当前没有待完成的登录窗口'})
                login_finish.set()
                return self.reply_json(200,{'ok':True,'message':'正在关闭浏览器并保存登录状态'})
            if self.path == '/auto/reset':
                with lock:
                    if busy: return self.reply_json(409,{'error':'任务正在运行，请先结束'})
                    for j in queue:
                        for path in ('source',):  # 只清理临时上传文件，绝不删除用户下载目录里的结果
                            if j.get(path):
                                try: Path(j[path]).unlink(missing_ok=True)
                                except Exception: pass
                    queue.clear(); stop_flag=False; finished=False; message='等待上传任务'
                return self.reply_json(200,{'ok':True})
            if self.path == '/auto/add':
                payload=json.loads(self.body(limit=LIMIT*2))
                jid=str(payload.get('id',''))
                name=str(payload.get('name','image.png'))[:180]
                match=re.fullmatch(r'[A-Za-z0-9_-]{3,50}', jid)
                if not match: raise ValueError('任务 ID 不合法')
                data=payload.get('data','')
                if not isinstance(data,str) or ',' not in data: raise ValueError('缺少图片数据')
                raw=base64.b64decode(data.split(',',1)[1],validate=True)
                if len(raw)>LIMIT: raise ValueError('图片超过45MB')
                ext=Path(name).suffix.lower()
                if ext not in ('.jpg','.jpeg','.png','.webp'): ext='.png'
                with lock:
                    if busy or len(queue)>=100: return self.reply_json(409,{'error':'已有任务运行或超过100张'})
                    if any(x['id']==jid for x in queue): return self.reply_json(409,{'error':'任务ID重复'})
                    file_path=Path(workdir.name)/(jid+ext)
                    file_path.write_bytes(raw)
                    queue.append({'id':jid,'name':name,'source':str(file_path),'status':'ready','message':'等待上传'})
                return self.reply_json(200,{'ok':True,'count':len(queue)})
            if self.path == '/auto/go':
                with lock:
                    if busy: return self.reply_json(409,{'error':'已运行'})
                    if login_active: return self.reply_json(409,{'error':'请先点击「完成登录并保存」，等待登录窗口关闭'})
                    if not queue: return self.reply_json(400,{'error':'队列为空'})
                    busy, finished, stop_flag = True, False, False
                    message='正在启动 Edge / Chrome'
                threading.Thread(target=run_automation,daemon=True).start()
                return self.reply_json(200,{'ok':True})
            if self.path == '/auto/stop':
                with lock: stop_flag=True;message='已请求停止，等待正在进行的网页操作结束'
                return self.reply_json(200,{'ok':True})
            return self.reply_json(404,{'error':'not found'})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
            return self.reply_json(400,{'error':str(e)})
        except Exception as e:
            return self.reply_json(500,{'error':str(e)})


from patchi_download_stable import install as _install_stable_download
_install_stable_download(globals())

if __name__=='__main__':
    try:
        local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with local.open(f'http://{HOST}:{PORT}/health', timeout=1) as response:
            current = json.load(response)
        if (current.get('service') == 'patchi-helper'
                and current.get('automationProtocol') == AUTOMATION_PROTOCOL
                and current.get('appVersion') == '2026.10.16.11'):
            print('当前修复版已在运行，正在打开编辑器。')
            if os.getenv('PAQI_NO_BROWSER') != '1': webbrowser.open(f'http://{HOST}:{PORT}/')
            sys.exit(0)
    except (OSError, ValueError):
        pass
    print('帕琪工坊 · 自动抠图浏览器助手')
    print(f'本机访问地址 http://{HOST}:{PORT}/health')
    print('请保持此窗口打开，浏览器将真实访问抠抠图网站。')
    print('首次若需网站登录，可点击「打开登录窗口」完成一次登录；批次后台沿用工坊持久浏览器登录数据，不开空白窗口。')
    print('验证码/会员下载限制仍须遵守；失败会保留原图。')
    try:
        server=ThreadingHTTPServer((HOST,PORT),Handler)
        print(f'下载目录：{DOWNLOAD_DIR}',flush=True)
        print(f'编辑器地址：http://{HOST}:{PORT}/',flush=True)
        if os.getenv('PAQI_NO_BROWSER')!='1':
            threading.Timer(1.2,lambda:webbrowser.open(f'http://{HOST}:{PORT}/')).start()
        server.serve_forever()
    except KeyboardInterrupt: print('已安全停止')
    except OSError as exc:
        print('\n[启动失败] 本地端口 8765 无法使用：' + str(exc))
        print('可能是旧版帕琪工坊助手仍在运行；旧版不支持 /gemini/product，会报 not found。')
        print('请先关闭旧 Python 助手，或用命令 netstat -ano | findstr :8765 检查占用该端口的 PID，再重新启动。')
        print('不要删除 %LOCALAPPDATA%\\帕琪工坊，这里保存了登录状态和 Gemini 配置。')
        sys.exit(1)
