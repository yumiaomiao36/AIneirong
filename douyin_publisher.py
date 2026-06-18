#!/usr/bin/env python3
"""
抖音创作者中心 — Playwright 半自动发布器
职责：上传视频 + 填写标题/描述/话题标签，停在发布按钮前等人工确认。

调用方式（server.py 通过 subprocess.Popen 启动）：
  /usr/bin/python3 douyin_publisher.py --task-id TASK_ID \
      --video-path /path/to/video.mp4 \
      --title "视频标题" \
      --body "描述文字" \
      --hashtags "标签1,标签2"
      
状态文件：publish_tasks/{task_id}.json
可能的状态：starting → launching_browser → navigating → (needs_login → retrying)
            → opening_upload → uploading_video → processing_video
            → filling_title → filling_body → adding_hashtags → draft_ready | failed
"""
import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeoutError

# 项目根目录
PROJECT_DIR = Path(__file__).parent.resolve()
TASKS_DIR = PROJECT_DIR / 'publish_tasks'
# Playwright 持久化用户目录，复用登录态
USER_DATA_DIR = PROJECT_DIR / '.playwright_profile' / 'douyin'
RUN_USER_DATA_ROOT = PROJECT_DIR / '.playwright_profile' / 'douyin_runs'


def write_status(task_id: str, status: str, **kwargs):
    """写入任务状态 JSON 文件，供 server.py 的 /publish/status 端点读取。"""
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        'task_id': task_id,
        'status': status,
        'updated_at': time.time(),
        **kwargs
    }
    with open(TASKS_DIR / f'{task_id}.json', 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f'[STATUS] {status}' + (f' | {kwargs}' if kwargs else ''))


def show_rpa_overlay(page, title: str, message: str = '', phase: str = 'running'):
    """Show a persistent status overlay inside the controlled Douyin browser window."""
    try:
        page.evaluate(
            '''({ title, message, phase }) => {
                const state = { title, message, phase, updatedAt: Date.now() };
                window.__agentflowRpaOverlayState = state;

                const ensure = () => {
                    const current = window.__agentflowRpaOverlayState || state;
                    let style = document.getElementById('agentflowRpaOverlayStyle');
                    if (!style) {
                        style = document.createElement('style');
                        style.id = 'agentflowRpaOverlayStyle';
                        style.textContent = `
                            #agentflowRpaOverlay {
                                position: fixed !important;
                                left: 0 !important;
                                right: 0 !important;
                                bottom: 0 !important;
                                z-index: 2147483647 !important;
                                width: 100vw !important;
                                min-height: 34px !important;
                                max-height: 40px !important;
                                box-sizing: border-box !important;
                                padding: 8px 14px !important;
                                border-radius: 0 !important;
                                background: rgba(17, 24, 39, .96) !important;
                                color: #fff !important;
                                box-shadow: 0 -8px 22px rgba(0,0,0,.18) !important;
                                font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif !important;
                                pointer-events: none !important;
                                display: flex !important;
                                align-items: center !important;
                                gap: 12px !important;
                                overflow: hidden !important;
                            }
                            #agentflowRpaOverlay .agentflow-rpa-row {
                                display: flex !important;
                                align-items: center !important;
                                gap: 10px !important;
                                margin-bottom: 0 !important;
                                flex: 0 0 auto !important;
                            }
                            #agentflowRpaOverlay .agentflow-rpa-dot {
                                width: 10px !important;
                                height: 10px !important;
                                border-radius: 999px !important;
                                flex: 0 0 10px !important;
                                background: #38bdf8 !important;
                                box-shadow: 0 0 0 6px rgba(56,189,248,.16) !important;
                                animation: agentflowRpaPulse 1.1s ease-in-out infinite !important;
                            }
                            #agentflowRpaOverlay[data-phase="ready"] .agentflow-rpa-dot {
                                background: #22c55e !important;
                                box-shadow: 0 0 0 6px rgba(34,197,94,.16) !important;
                            }
                            #agentflowRpaOverlay[data-phase="warn"] .agentflow-rpa-dot {
                                background: #f59e0b !important;
                                box-shadow: 0 0 0 6px rgba(245,158,11,.18) !important;
                            }
                            #agentflowRpaOverlay[data-phase="error"] .agentflow-rpa-dot {
                                background: #ef4444 !important;
                                box-shadow: 0 0 0 6px rgba(239,68,68,.18) !important;
                            }
                            #agentflowRpaOverlay .agentflow-rpa-title {
                                font-size: 13px !important;
                                line-height: 1 !important;
                                font-weight: 800 !important;
                                color: #fff !important;
                                white-space: nowrap !important;
                            }
                            #agentflowRpaOverlay .agentflow-rpa-message {
                                font-size: 13px !important;
                                line-height: 1 !important;
                                color: rgba(255,255,255,.86) !important;
                                white-space: nowrap !important;
                                overflow: hidden !important;
                                text-overflow: ellipsis !important;
                                flex: 1 1 auto !important;
                            }
                            #agentflowRpaOverlay .agentflow-rpa-foot {
                                margin-top: 0 !important;
                                font-size: 12px !important;
                                line-height: 1 !important;
                                color: rgba(255,255,255,.58) !important;
                                white-space: nowrap !important;
                                flex: 0 0 auto !important;
                            }
                            @keyframes agentflowRpaPulse {
                                0%, 100% { transform: scale(1); opacity: 1; }
                                50% { transform: scale(.72); opacity: .65; }
                            }
                        `;
                        (document.head || document.documentElement).appendChild(style);
                    }

                    let overlay = document.getElementById('agentflowRpaOverlay');
                    if (!overlay) {
                        overlay = document.createElement('div');
                        overlay.id = 'agentflowRpaOverlay';
                        (document.body || document.documentElement).appendChild(overlay);
                    }
                    overlay.setAttribute('data-phase', current.phase || 'running');
                    overlay.innerHTML = `
                        <div class="agentflow-rpa-row">
                            <div class="agentflow-rpa-dot"></div>
                            <div class="agentflow-rpa-title"></div>
                        </div>
                        <div class="agentflow-rpa-message"></div>
                        <div class="agentflow-rpa-foot"></div>
                    `;
                    overlay.querySelector('.agentflow-rpa-title').textContent = current.title || '正在执行发布流程';
                    overlay.querySelector('.agentflow-rpa-message').textContent = current.message || '';
                    overlay.querySelector('.agentflow-rpa-foot').textContent = current.phase === 'ready'
                        ? '可能需要手机验证码，请手动完成'
                        : '请勿接管';
                };

                window.__agentflowRefreshRpaOverlay = ensure;
                ensure();
                if (!window.__agentflowRpaOverlayTimer) {
                    window.__agentflowRpaOverlayTimer = window.setInterval(() => {
                        try {
                            if (location.href.includes('/content/manage')) {
                                const overlay = document.getElementById('agentflowRpaOverlay');
                                if (overlay) overlay.remove();
                                const guide = document.getElementById('agentflowPublishGuide');
                                if (guide) guide.remove();
                                const style = document.getElementById('agentflowPublishGuideStyle');
                                if (style) style.remove();
                                return;
                            }
                            ensure();
                        } catch (e) {}
                    }, 700);
                }
            }''',
            {'title': title, 'message': message, 'phase': phase},
        )
    except Exception:
        pass


def hide_rpa_overlay(page):
    try:
        page.evaluate(
            '''() => {
                if (window.__agentflowRpaOverlayTimer) {
                    clearInterval(window.__agentflowRpaOverlayTimer);
                    window.__agentflowRpaOverlayTimer = null;
                }
                const overlay = document.getElementById('agentflowRpaOverlay');
                if (overlay) overlay.remove();
                const style = document.getElementById('agentflowRpaOverlayStyle');
                if (style) style.remove();
                window.__agentflowRpaOverlayState = null;
            }'''
        )
    except Exception:
        pass


def cleanup_publish_overlays(page):
    """Remove all Agent Workflow handoff overlays from the controlled page."""
    hide_rpa_overlay(page)
    try:
        page.evaluate(
            '''() => {
                const ids = [
                    'agentflowPublishGuide',
                    'agentflowPublishGuideStyle',
                    'agentflowRpaOverlay',
                    'agentflowRpaOverlayStyle'
                ];
                for (const id of ids) {
                    const el = document.getElementById(id);
                    if (el) el.remove();
                }
                window.__agentflowRpaOverlayState = null;
            }'''
        )
    except Exception:
        pass


def browser_launch_kwargs(user_data_dir: Path):
    mark_profile_clean(user_data_dir)
    return {
        'user_data_dir': str(user_data_dir),
        'headless': False,
        # Keep the CSS viewport close to the real on-screen browser area.
        # If this is taller than the user's display, Playwright screenshots see
        # content that the user cannot actually see, which breaks handoff.
        'viewport': {'width': 1440, 'height': 740},
        'user_agent': (
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/125.0.0.0 Safari/537.36'
        ),
        'locale': 'zh-CN',
        'args': [
            '--no-proxy-server',
            '--window-size=1440,820',
            '--window-position=0,0',
            '--disable-session-crashed-bubble',
            '--hide-crash-restore-bubble',
            '--disable-restore-session-state',
        ],
    }


def mark_profile_clean(user_data_dir: Path):
    """Avoid Chromium's "restore pages" bubble after an unclean previous close."""
    try:
        user_data_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    candidates = [
        user_data_dir / 'Default' / 'Preferences',
        user_data_dir / 'Local State',
    ]
    for path in candidates:
        try:
            data = {}
            if path.exists() and path.stat().st_size > 0:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            if path.name == 'Preferences':
                data.setdefault('profile', {})
                data['profile']['exit_type'] = 'Normal'
                data['profile']['exited_cleanly'] = True
                data['profile']['exit_time'] = '0'
            else:
                data.setdefault('profile', {})
                data['profile']['exited_cleanly'] = True
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            continue


def copy_profile_for_task(task_id: str) -> Path:
    """Use an isolated profile when the shared profile is already open."""
    RUN_USER_DATA_ROOT.mkdir(parents=True, exist_ok=True)
    task_profile = RUN_USER_DATA_ROOT / task_id
    if task_profile.exists():
        shutil.rmtree(task_profile, ignore_errors=True)

    def ignore_profile_noise(_dir, names):
        ignored = set()
        prefixes = (
            'Singleton', 'Crashpad', 'BrowserMetrics', 'ShaderCache',
            'GrShaderCache', 'GraphiteDawnCache', 'GPUCache', 'Code Cache',
            'DawnCache', 'Cache', 'component_crx_cache',
        )
        for name in names:
            if name.startswith(prefixes):
                ignored.add(name)
        return ignored

    if USER_DATA_DIR.exists():
        try:
            shutil.copytree(USER_DATA_DIR, task_profile, ignore=ignore_profile_noise)
        except Exception:
            task_profile.mkdir(parents=True, exist_ok=True)
    else:
        task_profile.mkdir(parents=True, exist_ok=True)
    return task_profile


def launch_publish_context(p, task_id: str):
    try:
        return p.chromium.launch_persistent_context(**browser_launch_kwargs(USER_DATA_DIR))
    except Exception as first_error:
        task_profile = copy_profile_for_task(task_id)
        write_status(
            task_id,
            'launching_browser_retry',
            message='检测到发布浏览器配置被占用，已切换到本次任务独立窗口重新启动。',
            detail=str(first_error)[:500],
        )
        try:
            return p.chromium.launch_persistent_context(**browser_launch_kwargs(task_profile))
        except Exception as second_error:
            write_status(
                task_id,
                'failed',
                error='发布浏览器启动失败：' + str(second_error),
                first_error=str(first_error)[:1000],
            )
            raise


def get_user_window_metrics(page):
    """Return browser/window metrics useful for user-visible handoff checks."""
    try:
        return page.evaluate('''() => ({
            innerWidth: window.innerWidth,
            innerHeight: window.innerHeight,
            outerWidth: window.outerWidth,
            outerHeight: window.outerHeight,
            screenX: window.screenX,
            screenY: window.screenY,
            devicePixelRatio: window.devicePixelRatio || 1,
            screen: {
                width: window.screen && window.screen.width || 0,
                height: window.screen && window.screen.height || 0,
                availWidth: window.screen && window.screen.availWidth || 0,
                availHeight: window.screen && window.screen.availHeight || 0,
            },
        })''')
    except Exception:
        return {}


def force_browser_window_visible(page, task_id: str, preferred_width: int = 1440, preferred_height: int = 740):
    """Move the controlled Chromium window into the user's visible screen area."""
    metrics = get_user_window_metrics(page)
    screen = metrics.get('screen') or {}
    avail_width = int(screen.get('availWidth') or preferred_width)
    avail_height = int(screen.get('availHeight') or (preferred_height + 100))
    safe_width = int(min(preferred_width, max(1180, avail_width - 20)))
    safe_height = int(min(preferred_height, max(620, avail_height - 120)))
    window_height = safe_height + 90
    bounds = {
        'left': 0,
        'top': 0,
        'width': safe_width,
        'height': min(window_height, max(700, avail_height - 10)),
    }

    cdp_result = None
    try:
        session = page.context.new_cdp_session(page)
        window_info = session.send('Browser.getWindowForTarget')
        window_id = window_info.get('windowId')
        if window_id is not None:
            try:
                session.send('Browser.setWindowBounds', {
                    'windowId': window_id,
                    'bounds': {'windowState': 'normal'},
                })
                time.sleep(0.2)
            except Exception:
                pass
            session.send('Browser.setWindowBounds', {
                'windowId': window_id,
                'bounds': bounds,
            })
            cdp_result = {'windowId': window_id, 'bounds': bounds}
            time.sleep(0.5)
    except Exception as e:
        cdp_result = {'error': str(e)[:300], 'bounds': bounds}

    try:
        page.set_viewport_size({'width': safe_width, 'height': safe_height})
    except Exception:
        pass
    try:
        page.evaluate('''({ width, height }) => {
            try { window.moveTo(0, 0); } catch (e) {}
            try { window.resizeTo(width, height + 90); } catch (e) {}
        }''', {'width': safe_width, 'height': safe_height})
    except Exception:
        pass

    final_metrics = get_user_window_metrics(page)
    write_status(task_id, 'browser_window_visible',
                 message='已把发布浏览器窗口移动到屏幕可见区域。',
                 viewport={'width': safe_width, 'height': safe_height},
                 metrics=final_metrics,
                 cdp=cdp_result)
    return {'viewport': {'width': safe_width, 'height': safe_height}, 'metrics': final_metrics, 'cdp': cdp_result}


def prepare_browser_window(page, task_id: str):
    """尽量把创作者中心窗口调整到足够大的尺寸，避免发布按钮被窄窗口遮住。"""
    write_status(task_id, 'resizing_browser',
                 message='正在放大发布浏览器窗口，避免发布按钮被页面遮挡。')
    show_rpa_overlay(
        page,
        '正在准备发布浏览器',
        '正在放大发布窗口并清理浏览器恢复提示，请暂时不要操作这个浏览器。',
    )
    result = force_browser_window_visible(page, task_id)
    viewport = result.get('viewport') or {'width': 1440, 'height': 740}
    write_status(task_id, 'browser_viewport_ready',
                 message='发布浏览器视口已调整为用户可见范围。',
                 viewport=viewport,
                 metrics=result.get('metrics') or {})


def is_logged_in(page) -> bool:
    """
    综合判断是否已登录抖音创作者中心。
    1. 检查 URL 是否在登录/认证域
    2. 检查页面标题
    3. 检查页面上是否存在登录/验证弹窗（短信验证码、手机验证等）
    """
    url = page.url
    # 登录相关路径
    login_indicators = ['login', 'passport', 'sso.douyin.com', 'open.douyin.com']
    if any(kw in url for kw in login_indicators):
        return False

    # 页面标题含登录提示
    title = page.title()
    if '登录' in title and '创作者' not in title:
        return False

    # 检查页面上是否存在登录/验证弹窗（短信验证码、手机验证、登录框等）
    modal_indicators = [
        'text=短信验证码',
        'text=手机验证',
        'text=账号登录',
        'text=扫码登录',
        'text=验证码登录',
        'text=登录 douyin.com',
        'text=登录 抖音',
        'text=登录后',
        'input[placeholder*="验证码"]',
        'input[placeholder*="手机号"]',
    ]
    try:
        for sel in modal_indicators:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible():
                return False
    except Exception:
        pass

    return True


def wait_for_login(page, task_id: str, timeout_seconds: int = 120) -> bool:
    """
    等待用户手动登录（包括短信验证码）。
    轮询检测页面是否已完全进入创作者后台，最长等待 timeout_seconds 秒。
    """
    write_status(task_id, 'needs_login',
                 message='请在弹出的 Chrome 窗口中登录抖音创作者中心（含短信验证码）。'
                         '登录后发布流程会自动继续。')
    show_rpa_overlay(
        page,
        '需要登录抖音账号',
        '请在当前浏览器完成抖音创作者中心登录。登录成功后系统会自动继续发布流程。',
        phase='warn',
    )

    start = time.time()
    while time.time() - start < timeout_seconds:
        try:
            page.wait_for_load_state('networkidle', timeout=5000)
        except PwTimeoutError:
            pass

        if is_logged_in(page):
            write_status(task_id, 'logged_in', message='登录成功，继续发布流程')
            show_rpa_overlay(
                page,
                '登录成功',
                '系统检测到已登录，正在继续打开上传流程。',
            )
            time.sleep(2)  # 等页面完全加载
            return True

        # 检测页面是否关闭
        try:
            _ = page.url  # 探测
        except Exception:
            write_status(task_id, 'login_cancelled',
                         message='浏览器窗口已关闭，登录取消。请重新发起发布。')
            return False

        time.sleep(3)

    write_status(task_id, 'login_timeout',
                 message=f'登录等待超时（{timeout_seconds}秒）。请重新发起发布并尽快登录。')
    show_rpa_overlay(
        page,
        '登录等待超时',
        '长时间没有检测到登录完成，请重新发起发布并尽快完成登录。',
        phase='error',
    )
    return False


def navigate_to_upload(page, task_id: str) -> bool:
    """
    导航到内容上传页面。优先走抖音真实入口：高清发布 → 发布视频。
    返回 True 表示成功进入上传页或已进入发布表单页。
    失败时保存当前页面截图。
    """
    debug_dir = Path(__file__).parent / 'publish_debug'
    try:
        show_rpa_overlay(
            page,
            '正在打开抖音创作者中心',
            '系统会走“高清发布 → 发布视频”的真实路径，请先不要接管鼠标和键盘。',
        )
        page.goto('https://creator.douyin.com/creator-micro/home', wait_until='networkidle', timeout=30000)
        show_rpa_overlay(
            page,
            '正在进入视频发布入口',
            '已打开创作者中心首页，正在选择“高清发布 → 发布视频”。',
        )
        time.sleep(2)
        write_status(task_id, 'navigated_home', url=page.url, title=page.title()[:80])
        if open_video_publish_entry(page, task_id):
            return True
    except Exception as e:
        write_status(task_id, 'home_entry_skipped', message=f'首页发布入口未打开，改用直达入口：{str(e)[:200]}')

    # 兜底：直达上传/发布页。抖音偶尔会把 publish/upload 重定向到 post/video。
    urls_to_try = [
        'https://creator.douyin.com/creator-micro/content/publish',
        'https://creator.douyin.com/creator-micro/content/upload',
        'https://creator.douyin.com/creator-micro/content/post/video?enter_from=publish_page',
    ]
    for url in urls_to_try:
        try:
            show_rpa_overlay(
                page,
                '正在打开上传页',
                '真实入口未完成时会使用备用入口进入上传页，页面短暂空白或加载属正常。',
            )
            page.goto(url, wait_until='networkidle', timeout=30000)
            show_rpa_overlay(
                page,
                '已进入上传流程',
                '正在等待上传页控件渲染完成，随后会自动选择视频文件。',
            )
            time.sleep(3)
            write_status(task_id, 'navigated_upload', url=page.url, title=page.title()[:80])
            return True
        except Exception:
            continue

    # 全部失败，截图
    debug_dir.mkdir(exist_ok=True)
    try:
        page.screenshot(path=str(debug_dir / f'{task_id}_nav_fail.png'))
    except Exception:
        pass
    write_status(task_id, 'failed',
                 error=f'无法导航到抖音发布页面。当前 URL: {page.url}')
    return False


def open_video_publish_entry(page, task_id: str) -> bool:
    """从左侧“高清发布”下拉里选择“发布视频”。"""
    write_status(task_id, 'opening_publish_menu',
                 message='正在从“高清发布”下拉选择“发布视频”。')
    show_rpa_overlay(
        page,
        '正在选择发布视频入口',
        '系统正在展开“高清发布”菜单并点击“发布视频”，请不要移动鼠标。',
    )
    publish_triggers = [
        'button:has-text("高清发布")',
        'div:has-text("高清发布")',
        'span:has-text("高清发布")',
    ]
    trigger = None
    for sel in publish_triggers:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                trigger = loc
                break
        except Exception:
            continue
    if not trigger:
        return False

    try:
        trigger.hover(timeout=5000)
        time.sleep(0.6)
    except Exception:
        pass
    try:
        trigger.click(timeout=5000)
        time.sleep(0.8)
    except Exception:
        pass

    menu_items = [
        'text=发布视频',
        'button:has-text("发布视频")',
        '[role="menuitem"]:has-text("发布视频")',
        'div:has-text("发布视频")',
        'span:has-text("发布视频")',
    ]
    for sel in menu_items:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                try:
                    with page.expect_navigation(wait_until='networkidle', timeout=15000):
                        loc.click(timeout=5000)
                except Exception:
                    loc.click(timeout=5000)
                    try:
                        page.wait_for_load_state('networkidle', timeout=15000)
                    except Exception:
                        pass
                time.sleep(2)
                write_status(task_id, 'opening_upload', url=page.url,
                             message='已通过“高清发布 → 发布视频”进入上传流程。')
                show_rpa_overlay(
                    page,
                    '已进入上传页',
                    '正在等待上传控件加载，接下来会自动把视频文件交给抖音。',
                )
                return True
        except Exception:
            continue
    return False


def dismiss_overlays(page, task_id: str):
    """关闭页面上可能遮挡上传区的弹窗/浮窗（共创中心公告、发文助手等）。"""
    write_status(task_id, 'dismissing_popups')
    show_rpa_overlay(
        page,
        '正在清理页面弹窗',
        '如果页面出现公告或提示，系统会尝试关闭，避免遮挡上传和填写区域。',
    )

    # 1. 「新增"共创中心"模块」公告弹窗 — 红色"我知道了"按钮
    for sel in [
        'button:has-text("我知道了")',
        '.btn:has-text("我知道了")',
        'span:has-text("我知道了")',
    ]:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible():
                el.click(timeout=3000)
                time.sleep(1)
                break
        except Exception:
            continue

    # 2. 「发文助手」浮窗 — "完成"按钮或关闭箭头
    for sel in [
        'button:has-text("完成")',
        'span:has-text("完成")',
        '[class*="assistant"] button',
        '[class*="AssistClose"]',
        '[class*="close"]:near(span:has-text("发文助手"))',
        'svg[class*="close"]',
    ]:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible():
                el.click(timeout=3000)
                time.sleep(1)
                break
        except Exception:
            continue

    # 3. 通用遮罩/蒙层关闭（兜底）
    for sel in [
        '[class*="mask"] [class*="close"]',
        '[class*="modal"] [class*="close"]',
        '[class*="popup"] [class*="close"]',
    ]:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible():
                el.click(timeout=2000)
                time.sleep(0.5)
        except Exception:
            continue

    write_status(task_id, 'popups_dismissed')
    show_rpa_overlay(
        page,
        '弹窗清理完成',
        '继续执行发布流程，请等待下一步提示。',
    )


def dismiss_browser_restore_hint(page, task_id: str):
    """Best-effort dismissal for Chromium restore UI and restore page prompts."""
    write_status(task_id, 'dismissing_browser_restore')
    try:
        page.keyboard.press('Escape')
        time.sleep(0.3)
    except Exception:
        pass
    for sel in [
        'button:has-text("不恢复")',
        'button:has-text("取消")',
        'button:has-text("关闭")',
        'button:has-text("不用了")',
        'text=要恢复页面吗',
        'text=恢复页面',
    ]:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                if 'text=' not in sel:
                    loc.click(timeout=2000)
                else:
                    page.keyboard.press('Escape')
                time.sleep(0.5)
                break
        except Exception:
            continue


def wait_for_post_video_page(page, task_id: str, timeout_seconds: int = 120) -> bool:
    """After upload, Douyin navigates to content/post/video; wait for the new page state."""
    write_status(task_id, 'waiting_post_video_page',
                 message='上传后等待抖音跳转到作品发布编辑页...')
    show_rpa_overlay(
        page,
        '等待抖音跳转到编辑页',
        '视频提交后抖音会自动从上传页跳到发布编辑页，这段时间页面可能看起来没变化。',
    )
    start = time.time()
    last_url = ''
    while time.time() - start < timeout_seconds:
        try:
            last_url = page.url
            if 'content/post/video' in last_url:
                try:
                    page.wait_for_load_state('domcontentloaded', timeout=8000)
                except Exception:
                    pass
                markers = [
                    'text=发布设置',
                    'text=自主声明',
                    'text=添加作品简介',
                    'input[placeholder*="标题"]',
                    'textarea[placeholder*="简介"]',
                    'button:has-text("发布")',
                ]
                for sel in markers:
                    try:
                        loc = page.locator(sel)
                        if loc.count() > 0 and loc.first.is_visible():
                            write_status(task_id, 'post_video_page_ready',
                                         message='已进入作品发布编辑页。',
                                         url=last_url)
                            show_rpa_overlay(
                                page,
                                '已进入发布编辑页',
                                '正在等待页面稳定，然后填写标题、简介和话题。',
                            )
                            time.sleep(1)
                            return True
                    except Exception:
                        continue
            else:
                body = page.locator('body').inner_text(timeout=2000)
                if '发布设置' in body and ('暂存离开' in body or '添加作品简介' in body):
                    write_status(task_id, 'post_video_page_ready',
                                 message='检测到发布编辑表单。',
                                 url=last_url)
                    show_rpa_overlay(
                        page,
                        '已检测到发布表单',
                        '正在等待页面稳定，然后继续填写内容。',
                    )
                    return True
        except Exception:
            pass
        time.sleep(2)
    write_status(task_id, 'post_video_page_timeout',
                 message='上传后未等到作品发布编辑页。',
                 url=last_url)
    return False


def wait_for_publish_page_stable(page, task_id: str, timeout_seconds: int = 90) -> bool:
    """Wait until Douyin's checker/toasts stop moving the page before final scroll."""
    write_status(task_id, 'waiting_publish_page_stable',
                 message='等待发文助手检测、网络提示和页面回弹稳定...')
    show_rpa_overlay(
        page,
        '等待发布页稳定',
        '发文助手检测、封面推荐或网络提示可能会让页面回弹。系统正在等待它稳定，请暂时不要接管。',
    )
    start = time.time()
    stable_hits = 0
    last_scroll = None
    last_issue = ''
    while time.time() - start < timeout_seconds:
        try:
            snapshot = page.evaluate('''() => {
                const text = (document.body.innerText || '').slice(0, 8000);
                return {
                    text,
                    y: window.scrollY || document.documentElement.scrollTop || document.body.scrollTop || 0,
                    url: location.href,
                };
            }''')
            text = snapshot.get('text') or ''
            has_network_toast = any(s in text for s in ['网络不佳', '请重试', '网络异常', '网络错误'])
            checking = '检测中' in text
            uploading = any(s in text for s in ['上传中', '处理中', '转码中'])
            if has_network_toast:
                last_issue = '页面提示网络不佳/请重试'
                show_rpa_overlay(
                    page,
                    '检测到网络提示',
                    '抖音页面提示网络不佳/请重试，系统会等待并尝试继续处理。',
                    phase='warn',
                )
                try:
                    retry = page.locator('button:has-text("重试"), text=重试').first
                    if retry.count() > 0 and retry.is_visible():
                        retry.click(timeout=2000)
                except Exception:
                    pass
                stable_hits = 0
            elif checking or uploading:
                last_issue = '发文助手或上传状态仍在检测/处理中'
                show_rpa_overlay(
                    page,
                    '发文助手仍在检测',
                    '页面正在检测作品或处理上传状态，等待完成后再定位发布按钮。',
                )
                stable_hits = 0
            else:
                y = snapshot.get('y')
                if last_scroll is None or abs(float(y) - float(last_scroll)) <= 2:
                    stable_hits += 1
                else:
                    stable_hits = 0
                last_scroll = y
                if stable_hits >= 3:
                    write_status(task_id, 'publish_page_stable',
                                 message='发布编辑页已稳定，可以定位最终发布按钮。',
                                 url=snapshot.get('url'))
                    show_rpa_overlay(
                        page,
                        '发布页已稳定',
                        '接下来会填写内容或定位底部真实发布按钮，请继续等待。',
                    )
                    return True
        except Exception as e:
            last_issue = str(e)[:200]
            stable_hits = 0
        time.sleep(2)
    write_status(task_id, 'publish_page_not_stable',
                 message='发布编辑页长时间未稳定，继续尝试定位按钮。',
                 reason=last_issue)
    return False


def upload_video(page, video_path: str, task_id: str) -> bool:
    """
    上传视频文件到抖音创作者中心。
    策略：先点击上传区触发 file_chooser → 失败则 JS 注入找到 <input type="file"> 直接 setInputFiles。
    """
    write_status(task_id, 'uploading_video')
    show_rpa_overlay(
        page,
        '正在准备选择视频文件',
        '上传页打开后可能短时间看起来没有动作。系统正在寻找上传控件，请勿操作鼠标和键盘。',
    )

    # 策略 0：页面若已渲染 file input，直接设置文件，优先选择视频 input。
    try:
        file_inputs = page.locator('input[type="file"]')
        total = file_inputs.count()
        preferred = []
        fallback = []
        for i in range(min(total, 20)):
            accept = (file_inputs.nth(i).get_attribute('accept') or '').lower()
            if 'image' in accept and 'video' not in accept:
                continue
            if 'video' in accept or not accept:
                preferred.append(i)
            else:
                fallback.append(i)
        for i in preferred + fallback:
            try:
                show_rpa_overlay(
                    page,
                    '正在提交视频文件',
                    '已找到抖音上传控件，正在把本地视频文件交给页面，请勿点击页面。',
                )
                file_inputs.nth(i).set_input_files(video_path)
                write_status(task_id, 'uploading_video_input', input_index=i)
                show_rpa_overlay(
                    page,
                    '视频文件已提交',
                    '正在等待抖音显示上传进度或自动跳转，请继续等待。',
                )
                return True
            except Exception:
                continue
    except Exception:
        pass

    # ========== 策略 1：点击 + file_chooser ==========
    try:
        show_rpa_overlay(
            page,
            '正在打开文件选择动作',
            '系统正在触发抖音上传区并选择视频文件，请勿移动鼠标。',
        )
        with page.expect_file_chooser(timeout=8000) as fc_info:
            clicked = _click_upload_zone(page)
            if not clicked:
                raise Exception('未找到可点击的上传入口')
        fc = fc_info.value
        show_rpa_overlay(
            page,
            '正在选择文件',
            '文件选择动作已触发，正在把视频文件交给抖音上传控件。',
        )
        fc.set_files(video_path)
        show_rpa_overlay(
            page,
            '视频文件已提交',
            '正在等待抖音显示上传进度或自动跳转，请继续等待。',
        )
        return True
    except Exception:
        pass  # 降级到策略 2

    # ========== 策略 2：JS 找到视频上传专用的 file input（排除封面图片的） ==========
    write_status(task_id, 'uploading_video_js')
    show_rpa_overlay(
        page,
        '正在使用备用上传方式',
        '常规文件选择未触发，系统正在直接定位页面里的视频上传控件。',
        phase='warn',
    )
    try:
        # 智能搜索：找上传区内的 file input，排除封面/图片上传的
        file_inputs = page.evaluate('''() => {
            const allInputs = document.querySelectorAll('input[type="file"]');
            const results = [];
            allInputs.forEach((el, i) => {
                // 检查是否在"点击上传"或"拖入此区域"附近
                let parent = el.parentElement;
                let nearUploadZone = false;
                let isCoverInput = false;
                for (let depth = 0; depth < 5 && parent; depth++) {
                    const text = (parent.textContent || '').slice(0, 200);
                    if (text.includes('点击上传') || text.includes('拖入此区域') || text.includes('视频')) {
                        nearUploadZone = true;
                    }
                    if (text.includes('封面')) {
                        isCoverInput = true;
                    }
                    parent = parent.parentElement;
                }
                // 排除明确是图片的
                if (el.accept && el.accept.includes('image')) isCoverInput = true;
                
                results.push({
                    index: i,
                    accept: el.accept || '',
                    nearUploadZone: nearUploadZone,
                    isCoverInput: isCoverInput,
                });
            });
            return results;
        }''')

        # 优先选：在上传区且不是封面的
        best = None
        for fi in (file_inputs or []):
            if fi['nearUploadZone'] and not fi['isCoverInput']:
                best = fi; break
        # 次选：不是封面的第一个
        if not best:
            for fi in (file_inputs or []):
                if not fi['isCoverInput']:
                    best = fi; break
        # 末选：第一个
        if not best and file_inputs:
            best = file_inputs[0]

        if best:
            show_rpa_overlay(
                page,
                '正在提交视频文件',
                '已通过备用方式找到上传控件，正在提交视频文件。',
            )
            page.locator('input[type="file"]').nth(best['index']).set_input_files(video_path)
            show_rpa_overlay(
                page,
                '视频文件已提交',
                '正在等待抖音上传/转码完成，请继续等待。',
            )
            return True

        # 策略 2b：找不到合适的 input，先点击上传区再在弹窗中设置
        show_rpa_overlay(
            page,
            '正在重新触发文件选择',
            '正在再次点击上传区并提交视频文件，请勿操作鼠标。',
        )
        with page.expect_file_chooser(timeout=5000) as fc_info:
            _click_upload_zone(page)
        fc_info.value.set_files(video_path)
        show_rpa_overlay(
            page,
            '视频文件已提交',
            '正在等待抖音上传/转码完成，请继续等待。',
        )
        return True

    except Exception:
        pass

    # ========== 全策略失败 ==========
    debug_dir = Path(__file__).parent / 'publish_debug'
    debug_dir.mkdir(exist_ok=True)
    screenshot_path = str(debug_dir / f'{task_id}_no_upload.png')
    html_path = str(debug_dir / f'{task_id}_page.html')
    try:
        page.screenshot(path=screenshot_path, full_page=False)
        html = page.content()
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(html[:50000])
    except Exception:
        pass

    write_status(task_id, 'failed',
                 error=f'找不到文件上传入口，页面结构可能已变化。'
                       f'截图: {screenshot_path}，页面: {html_path}')
    show_rpa_overlay(
        page,
        '未找到上传入口',
        '系统没有找到可用的视频上传控件，已保存调试截图。',
        phase='error',
    )
    return False


def _click_upload_zone(page) -> bool:
    """尝试点击上传区域。返回 True 表示点击成功（未必触发 file_chooser）。"""
    click_targets = [
        'text=点击上传',
        'text=或者将视频文件拖入此区域',
        'div:has-text("点击上传"):has-text("拖入此区域")',
        'div:has-text("上传视频"):has-text("视频文件")',
        '[class*="upload"]:has-text("点击上传")',
        '[class*="Upload"]:has-text("点击上传")',
        '[class*="uploader"]:has-text("点击上传")',
        'div[class*="upload-area"]',
    ]
    for sel in click_targets:
        loc = page.locator(sel)
        count = loc.count()
        for i in range(min(count, 5)):
            el = loc.nth(i)
            try:
                if el.is_visible():
                    el.click()
                    return True
            except Exception:
                continue

    # 兜底：遍历可见元素找含"上传"文本的
    all_visible = page.locator('div:visible, button:visible, span:visible')
    count = all_visible.count()
    for i in range(min(count, 200)):
        try:
            text = all_visible.nth(i).inner_text()
            if ('点击上传' in text or '上传视频' in text) and len(text) < 80:
                all_visible.nth(i).click()
                return True
        except Exception:
            continue

    return False


def wait_for_upload_complete(page, task_id: str, timeout_seconds: int = 300) -> bool:
    """
    等待视频上传及转码完成。
    判据：上传区"点击上传"文字消失 / 视频预览出现 / "视频预览功能"弹窗出现。
    不依赖进度条 CSS class（抖音动态渲染，类名不稳定）。
    """
    write_status(task_id, 'processing_video',
                 message='视频上传中，等待转码完成...')
    show_rpa_overlay(
        page,
        '等待视频上传/转码',
        '视频已交给抖音。页面可能短时间没有明显变化，系统正在等待上传进度、预览或跳转。',
    )

    start = time.time()

    while time.time() - start < timeout_seconds:
        elapsed = time.time() - start

        # 判据 1：上传区 "点击上传" 文字消失
        try:
            upload_hint = page.locator('text=点击上传').first
            if upload_hint.count() == 0 or not upload_hint.is_visible():
                if elapsed > 5:  # 至少等 5 秒避免刚点上传就误判
                    write_status(task_id, 'upload_complete',
                                 message='上传区文字已消失，视频上传完成')
                    show_rpa_overlay(
                        page,
                        '上传状态已变化',
                        '检测到上传区已变化，继续等待抖音进入发布编辑流程。',
                    )
                    time.sleep(2)
                    return True
        except Exception:
            pass

        # 判据 2：视频预览元素出现
        try:
            video_el = page.locator('video').first
            if video_el.count() > 0:
                write_status(task_id, 'upload_complete',
                             message='检测到视频预览元素')
                show_rpa_overlay(
                    page,
                    '检测到视频预览',
                    '视频已进入可编辑状态，正在继续发布流程。',
                )
                time.sleep(2)
                return True
        except Exception:
            pass

        # 判据 2b：进入可发布表单态，页面出现发布按钮或重新上传/更换视频。
        try:
            done_markers = [
                'button:has-text("发布")',
                'text=重新上传',
                'text=更换视频',
                'text=选择封面',
                'text=设置封面',
            ]
            for sel in done_markers:
                marker = page.locator(sel)
                if marker.count() > 0 and marker.first.is_visible():
                    write_status(task_id, 'upload_complete',
                                 message=f'检测到上传完成标记：{sel}')
                    show_rpa_overlay(
                        page,
                        '视频上传完成',
                        '检测到抖音上传完成标记，正在进入下一步。',
                    )
                    time.sleep(2)
                    return True
        except Exception:
            pass

        # 判据 3："视频预览功能" 弹窗出现（上传完成后的提示）
        try:
            popup = page.locator('text=视频预览功能').first
            if popup.count() > 0 and popup.is_visible():
                write_status(task_id, 'upload_complete',
                             message='检测到视频预览功能弹窗，上传完成')
                show_rpa_overlay(
                    page,
                    '视频上传完成',
                    '检测到抖音上传完成提示，正在进入下一步。',
                )
                time.sleep(2)
                return True
        except Exception:
            pass

        # 判据 4：进度百分比文本出现
        try:
            pct = page.locator('text=/\\d{1,3}%$/').first
            if pct.count() > 0 and pct.is_visible():
                # 有进度说明在上传中，继续等
                pass
        except Exception:
            pass

        if int(elapsed) and int(elapsed) % 30 == 0:
            write_status(task_id, 'processing_video',
                         message=f'视频上传/转码中，已等待 {int(elapsed)} 秒...')
            show_rpa_overlay(
                page,
                '仍在等待上传/转码',
                f'已等待 {int(elapsed)} 秒。只要没有失败提示，请继续等待，不要接管鼠标键盘。',
            )
        time.sleep(2)

    write_status(task_id, 'upload_timeout',
                 message=f'视频上传超时（{timeout_seconds}秒），请检查视频大小和网络状态')
    show_rpa_overlay(
        page,
        '上传等待超时',
        '长时间未检测到上传完成，请检查网络、视频大小或抖音页面提示。',
        phase='error',
    )
    return False


def fill_title_and_body(page, title: str, body: str, hashtags: str, task_id: str):
    """填写标题、描述和话题标签。"""
    if title:
        write_status(task_id, 'filling_title')
        show_rpa_overlay(
            page,
            '正在填写视频标题',
            '系统正在把标题写入抖音发布页，请不要手动点击输入框。',
        )
        try:
            filled = _fill_first_visible(
                page,
                [
                    'input[placeholder="填写作品标题，为作品获得更多流量"]',
                    'input[placeholder*="作品标题"]',
                    'input[placeholder*="标题"]',
                    '[class*="title"] input',
                    'input[class*="Title"]',
                ],
                title,
                exclude_placeholders=['付费', '请输入付费场景下的视频标题'],
                delay=30,
            )
            write_status(task_id, 'title_filled' if filled else 'title_fill_skipped',
                         message='标题已填写' if filled else '未找到可用标题输入框')
            time.sleep(0.5)
        except Exception as e:
            print(f'填写标题失败: {e}')

    if body:
        write_status(task_id, 'filling_body')
        show_rpa_overlay(
            page,
            '正在填写作品简介',
            '系统正在填写正文/简介内容，页面可能会出现话题推荐弹层。',
        )
        try:
            filled = _fill_first_visible(
                page,
                [
                    'textarea[placeholder="添加作品简介"]',
                    'textarea[placeholder*="作品简介"]',
                    'textarea[placeholder*="描述"]',
                    'textarea[placeholder*="简介"]',
                    '[class*="desc"] textarea',
                    '[class*="description"] textarea',
                    'div[contenteditable="true"]',
                ],
                body,
                delay=20,
            )
            write_status(task_id, 'body_filled' if filled else 'body_fill_skipped',
                         message='简介/正文已填写' if filled else '未找到可用简介输入框')
            time.sleep(0.5)
        except Exception as e:
            print(f'填写描述失败: {e}')

    if hashtags:
        write_status(task_id, 'adding_hashtags')
        show_rpa_overlay(
            page,
            '正在添加话题标签',
            '系统正在把话题追加到简介里，最多话题限制由抖音页面决定。',
        )
        tags = [t.strip().lstrip('#') for t in hashtags.split(',') if t.strip()]
        if tags:
            tag_text = ' ' + ' '.join(f'#{t}' for t in tags)
            try:
                # 话题标签通常追加到描述框末尾
                _append_first_visible(
                    page,
                    [
                        'textarea[placeholder="添加作品简介"]',
                        'textarea[placeholder*="作品简介"]',
                        'textarea[placeholder*="描述"]',
                        'textarea[placeholder*="简介"]',
                        '[class*="desc"] textarea',
                        'div[contenteditable="true"]',
                    ],
                    tag_text,
                    delay=50,
                )
            except Exception as e:
                print(f'添加话题标签失败: {e}')


def locate_publish_button_legacy(page, task_id: str) -> bool:
    """定位最终发布按钮。只有按钮真实进入 viewport 才返回 True。"""
    write_status(task_id, 'scrolling_publish_area',
                 message='正在定位发布表单容器，并滚动到最终发布按钮。')

    def locate_and_highlight():
        return page.evaluate('''() => {
            const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return rect.width > 4 && rect.height > 4 && style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0';
            };
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, '');
            const rectData = (el) => {
                const r = el.getBoundingClientRect();
                return { x:r.x, y:r.y, top:r.top, left:r.left, right:r.right, bottom:r.bottom, width:r.width, height:r.height };
            };
            const inViewport = (el) => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0
                    && r.top >= 0 && r.left >= 0
                    && r.bottom <= (window.innerHeight || document.documentElement.clientHeight)
                    && r.right <= (window.innerWidth || document.documentElement.clientWidth);
            };
            const isScrollable = (el) => {
                if (!el || el === document.body || el === document.documentElement) return false;
                const style = window.getComputedStyle(el);
                return /(auto|scroll)/.test(style.overflowY || '') && el.scrollHeight > el.clientHeight + 40;
            };
            const scrollParents = (el) => {
                const out = [];
                let cur = el && el.parentElement;
                while (cur && cur !== document.body) {
                    if (isScrollable(cur)) out.push(cur);
                    cur = cur.parentElement;
                }
                out.push(document.scrollingElement || document.documentElement);
                return out;
            };

            const all = Array.from(document.querySelectorAll('body *')).filter(visible);
            const textBlocks = all.map(el => ({ el, text: textOf(el), rect: el.getBoundingClientRect() }));
            const publishSetting = textBlocks
                .filter(x => x.text.includes('发布设置') || x.text.includes('谁可以看') || x.text.includes('保存权限') || x.text.includes('发布时间'))
                .sort((a, b) => (b.rect.top - a.rect.top) || (b.rect.height * b.rect.width - a.rect.height * a.rect.width))[0]?.el || null;

            const actionBars = textBlocks
                .filter(x => x.text.includes('暂存离开') && /(^|[^高清])发布/.test(x.text) && x.rect.width > 120 && x.rect.height > 30)
                .sort((a, b) => b.rect.top - a.rect.top)
                .map(x => x.el);

            const buttonLike = Array.from(document.querySelectorAll('button,[role="button"],a,div,span')).filter(el => {
                if (!visible(el)) return false;
                const t = textOf(el);
                if (t !== '发布') return false;
                const r = el.getBoundingClientRect();
                if (r.width < 40 || r.height < 24) return false;
                const context = textOf(el.closest('section,form,main,div') || el.parentElement || el);
                if (context.includes('高清发布') && !context.includes('暂存离开')) return false;
                return true;
            });

            const scoreButton = (el) => {
                const r = el.getBoundingClientRect();
                let score = r.top;
                const parentText = textOf(el.parentElement || el);
                const nearText = textOf(el.closest('section,form,main,div') || el.parentElement || el);
                if (parentText.includes('暂存离开')) score += 5000;
                if (nearText.includes('发布设置')) score += 1000;
                if (publishSetting && r.top >= publishSetting.getBoundingClientRect().top) score += 800;
                return score;
            };

            let target = null;
            if (actionBars.length) {
                for (const bar of actionBars) {
                    const inside = buttonLike.filter(btn => bar.contains(btn));
                    if (inside.length) {
                        inside.sort((a, b) => scoreButton(b) - scoreButton(a));
                        target = inside[0];
                        break;
                    }
                }
            }
            if (!target && buttonLike.length) {
                buttonLike.sort((a, b) => scoreButton(b) - scoreButton(a));
                target = buttonLike[0];
            }
            if (!target) {
                return {
                    found:false,
                    visible:false,
                    reason:'没有找到底部最终发布按钮候选',
                    publishSettingFound: !!publishSetting,
                    actionBarCount: actionBars.length,
                    buttonCount: buttonLike.length,
                    viewport:{ width:window.innerWidth, height:window.innerHeight },
                };
            }
            target = target.closest('button,[role="button"],a') || target;

            const parents = scrollParents(target);
            for (const parent of parents) {
                if (parent === document.scrollingElement || parent === document.documentElement || parent === document.body) {
                    const top = target.getBoundingClientRect().top + window.scrollY - Math.max(80, window.innerHeight * 0.55);
                    window.scrollTo({ top: Math.max(0, top), behavior: 'instant' });
                } else {
                    const pr = parent.getBoundingClientRect();
                    const tr = target.getBoundingClientRect();
                    parent.scrollTop += (tr.top - pr.top) - Math.max(40, parent.clientHeight * 0.55);
                }
            }
            target.scrollIntoView({ block: 'center', inline: 'center' });

            const oldGuide = document.getElementById('agentflowPublishGuide');
            if (oldGuide) oldGuide.remove();
            const oldStyle = document.getElementById('agentflowPublishGuideStyle');
            if (oldStyle) oldStyle.remove();
            const originalRect = rectData(target);
            target.setAttribute('data-agentflow-publish-target', 'true');
            target.setAttribute('data-agentflow-original-rect', JSON.stringify(originalRect));
            target.style.setProperty('position', 'fixed', 'important');
            target.style.setProperty('left', '24px', 'important');
            target.style.setProperty('bottom', '32px', 'important');
            target.style.setProperty('top', 'auto', 'important');
            target.style.setProperty('right', 'auto', 'important');
            target.style.setProperty('z-index', '2147483647', 'important');
            target.style.setProperty('min-width', '176px', 'important');
            target.style.setProperty('height', '48px', 'important');
            target.style.setProperty('display', 'inline-flex', 'important');
            target.style.setProperty('align-items', 'center', 'important');
            target.style.setProperty('justify-content', 'center', 'important');
            target.style.setProperty('font-size', '16px', 'important');
            target.style.setProperty('font-weight', '800', 'important');
            target.style.setProperty('background', '#ff2d55', 'important');
            target.style.setProperty('color', '#fff', 'important');
            target.style.setProperty('border-radius', '10px', 'important');
            target.style.setProperty('border', '0', 'important');
            target.style.setProperty('cursor', 'pointer', 'important');
            const visibleNow = inViewport(target);
            if (!visibleNow) {
                return {
                    found:true,
                    visible:false,
                    reason:'找到发布按钮，但滚动后仍未进入当前视口',
                    rect:rectData(target),
                    publishSettingFound: !!publishSetting,
                    actionBarCount: actionBars.length,
                    buttonCount: buttonLike.length,
                    viewport:{ width:window.innerWidth, height:window.innerHeight },
                };
            }

            const el = target;
            el.style.outline = '4px solid #ff2d55';
            el.style.boxShadow = '0 0 0 8px rgba(255,45,85,.25)';
            el.style.borderRadius = '8px';
            const guide = document.createElement('div');
            guide.id = 'agentflowPublishGuide';
            guide.innerHTML = '<div style="font-size:15px;font-weight:800;margin-bottom:4px;">已把真实发布按钮固定到左下角</div><div style="font-size:13px;line-height:1.5;">请检查封面、可见范围、声明后，点击左下角红色“发布”按钮。它就是抖音原页面的最终发布按钮。</div>';
            guide.style.cssText = [
                'position:fixed',
                'left:220px',
                'bottom:28px',
                'z-index:2147483647',
                'background:#111827',
                'color:white',
                'padding:12px 14px',
                'border-radius:10px',
                'box-shadow:0 10px 30px rgba(0,0,0,.25)',
                'max-width:360px',
                'font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif'
            ].join(';');
            document.body.appendChild(guide);
            const pulse = document.createElement('style');
            pulse.id = 'agentflowPublishGuideStyle';
            pulse.textContent = '[data-agentflow-publish-target="true"]{animation:agentflowPublishPulse 1.1s ease-in-out infinite!important;}@keyframes agentflowPublishPulse{0%,100%{filter:brightness(1)}50%{filter:brightness(1.18)}}';
            document.head.appendChild(pulse);
            return {
                found:true,
                visible:true,
                pinned:true,
                text: (el.innerText || el.textContent || '').trim(),
                disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
                rect: rectData(el),
                originalRect,
                publishSettingFound: !!publishSetting,
                actionBarCount: actionBars.length,
                buttonCount: buttonLike.length,
                viewport:{ width:window.innerWidth, height:window.innerHeight },
            };
        }''')

    last_check_text = ''
    last_result = None
    for attempt in range(30):
        time.sleep(1)
        try:
            found = locate_and_highlight()
            last_result = found
            if found and found.get('visible'):
                write_status(task_id, 'publish_button_located',
                             message='最终发布按钮已进入当前视口，并已高亮。请检查内容后点击该按钮。',
                             button=found)
                try:
                    debug_dir = Path(__file__).parent / 'publish_debug'
                    debug_dir.mkdir(exist_ok=True)
                    page.screenshot(path=str(debug_dir / f'{task_id}_publish_button_pinned.png'))
                except Exception:
                    pass
                return True
        except Exception:
            pass

        try:
            body_text = page.locator('body').inner_text(timeout=2000)
            if '检测中' in body_text:
                last_check_text = '发文助手仍在检测中，正在等待检测完成。'
                write_status(task_id, 'waiting_publish_check',
                             message=last_check_text,
                             attempt=attempt + 1)
            elif '封面缺失' in body_text or '双封面缺失' in body_text:
                last_check_text = '页面提示封面缺失，最终发布按钮可能暂未显示或不可用。'
        except Exception:
            pass

    try:
        debug_dir = Path(__file__).parent / 'publish_debug'
        debug_dir.mkdir(exist_ok=True)
        page.screenshot(path=str(debug_dir / f'{task_id}_publish_button_not_visible_viewport.png'))
        page.screenshot(path=str(debug_dir / f'{task_id}_publish_button_not_visible_full.png'), full_page=True)
    except Exception:
        pass
    write_status(task_id, 'publish_button_not_visible',
                 message='草稿已填写完成，但最终发布按钮没有进入当前视口。'
                         + (last_check_text or '请检查封面、声明、可见范围或右侧发文助手提示。'),
                 locator_result=last_result or {})
    return False


def locate_publish_button(page, task_id: str) -> bool:
    """Highlight the real final publish button in place, then verify it survives rerenders."""
    write_status(task_id, 'scrolling_publish_area',
                 message='正在定位发布表单底部操作区，并原位高亮最终发布按钮。')
    force_browser_window_visible(page, task_id)
    show_rpa_overlay(
        page,
        '正在定位发布按钮',
        '标题和视频已处理，系统正在滚动到页面底部并寻找真实“发布”按钮。请暂时不要接管。',
    )

    first = _locate_publish_button_once(page)
    if not first or not first.get('visible'):
        _save_publish_button_debug(page, task_id, 'publish_button_user_not_visible')
        write_status(task_id, 'publish_button_user_not_visible',
                     message='草稿已填写完成，但最终发布按钮没有进入用户安全可见区域。',
                     locator_result=first or {},
                     metrics=get_user_window_metrics(page))
        show_rpa_overlay(
            page,
            '暂未看到发布按钮',
            '草稿已填写，但系统没有把最终发布按钮带入用户安全可见区域，已保存调试截图。',
            phase='warn',
        )
        return False

    write_status(task_id, 'publish_button_located',
                 message='已原位高亮最终发布按钮，正在确认高亮不会被页面重绘覆盖。',
                 button=first)
    show_rpa_overlay(
        page,
        '发布按钮已找到',
        '真实“发布”按钮已被红框高亮，系统还会观察 5 秒，确认不会被页面重绘覆盖。',
    )
    stable = guard_and_verify_publish_button(page, task_id, timeout_seconds=15)
    if stable:
        _save_publish_button_debug(page, task_id, 'publish_button_highlighted')
        show_rpa_overlay(
            page,
            '现在可以接管并点击发布',
            '发布按钮红框高亮已稳定。请点击红框“发布”；如弹出手机验证码，请手动完成。',
            phase='ready',
        )
        return True

    _save_publish_button_debug(page, task_id, 'publish_button_unstable')
    write_status(task_id, 'publish_button_user_not_visible',
                 message='发布按钮没有稳定保持在用户安全可见区域，请保留浏览器窗口和调试截图。',
                 locator_result={},
                 metrics=get_user_window_metrics(page))
    show_rpa_overlay(
        page,
        '发布按钮未达到可交付状态',
        '系统没有确认你能稳定看到发布按钮，因此不会标记为草稿就绪。',
        phase='warn',
    )
    return False


def _locate_publish_button_once(page):
    return page.evaluate('''() => {
        const textOf = (el) => (el && (el.innerText || el.textContent || '').replace(/\\s+/g, '')) || '';
        const visible = (el) => {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            const s = window.getComputedStyle(el);
            return r.width > 4 && r.height > 4 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
        };
        const rectData = (el) => {
            const r = el.getBoundingClientRect();
            return { x:r.x, y:r.y, top:r.top, left:r.left, right:r.right, bottom:r.bottom, width:r.width, height:r.height };
        };
        const inViewport = (el) => {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0
                && r.top >= 0 && r.left >= 0
                && r.bottom <= (window.innerHeight || document.documentElement.clientHeight)
                && r.right <= (window.innerWidth || document.documentElement.clientWidth);
        };
        const viewportMeta = () => {
            const innerHeight = window.innerHeight || document.documentElement.clientHeight || 740;
            const innerWidth = window.innerWidth || document.documentElement.clientWidth || 1440;
            const topSafe = Math.min(140, Math.max(84, innerHeight * 0.16));
            const bottomSafe = Math.min(180, Math.max(128, innerHeight * 0.22));
            return {
                innerWidth,
                innerHeight,
                topSafe,
                bottomSafe,
                safeTop: topSafe,
                safeBottom: innerHeight - bottomSafe,
                screenY: window.screenY,
                outerHeight: window.outerHeight,
                screenAvailHeight: window.screen && window.screen.availHeight || 0,
                devicePixelRatio: window.devicePixelRatio || 1,
                zoom: document.documentElement.style.zoom || document.body.style.zoom || '1',
            };
        };
        const userSafeVisible = (el) => {
            const r = el.getBoundingClientRect();
            const meta = viewportMeta();
            return r.width > 0 && r.height > 0
                && r.left >= 0 && r.right <= meta.innerWidth
                && r.top >= meta.safeTop
                && r.bottom <= meta.safeBottom;
        };
        const isScrollable = (el) => {
            if (!el || el === document.body || el === document.documentElement) return false;
            const s = window.getComputedStyle(el);
            return /(auto|scroll)/.test(s.overflowY || '') && el.scrollHeight > el.clientHeight + 40;
        };
        const scrollParents = (el) => {
            const out = [];
            let cur = el && el.parentElement;
            while (cur && cur !== document.body) {
                if (isScrollable(cur)) out.push(cur);
                cur = cur.parentElement;
            }
            out.push(document.scrollingElement || document.documentElement);
            return out;
        };
        const buttons = Array.from(document.querySelectorAll('button')).filter(btn => {
            const text = textOf(btn);
            if (text !== '发布') return false;
            if (!visible(btn)) return false;
            const context = textOf(btn.closest('form,section,main,[class*="publish"],[class*="content"],[class*="form"],div') || btn.parentElement || btn);
            return context.includes('暂存离开') && !context.includes('高清发布');
        });

        let target = null;
        const actionContainers = Array.from(document.querySelectorAll('form,section,main,[class*="publish"],[class*="content"],[class*="form"],div'))
            .filter(el => visible(el))
            .map(el => ({ el, text:textOf(el), rect:el.getBoundingClientRect() }))
            .filter(x => x.text.includes('暂存离开') && x.text.includes('发布') && x.rect.width > 120 && x.rect.height > 30)
            .sort((a, b) => {
                const areaA = a.rect.width * a.rect.height;
                const areaB = b.rect.width * b.rect.height;
                return (areaA - areaB) || (b.rect.top - a.rect.top);
            });
        for (const c of actionContainers) {
            const inside = Array.from(c.el.querySelectorAll('button')).filter(btn => textOf(btn) === '发布' && visible(btn));
            if (inside.length) {
                inside.sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top);
                target = inside[0];
                break;
            }
        }
        if (!target && buttons.length) {
            buttons.sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top);
            target = buttons[0];
        }
        if (!target) {
            return {
                found:false,
                visible:false,
                reason:'没有找到“暂存离开”操作区内的发布 button',
                actionBarCount: actionContainers.length,
                buttonCount: buttons.length,
                viewport:{ width:window.innerWidth, height:window.innerHeight },
            };
        }

        target.scrollIntoView({ block:'center', inline:'center', behavior:'instant' });
        if (!userSafeVisible(target)) {
            for (const parent of scrollParents(target)) {
                const meta = viewportMeta();
                const wantedTop = meta.safeTop + (meta.safeBottom - meta.safeTop) * 0.48;
                if (parent === document.scrollingElement || parent === document.documentElement || parent === document.body) {
                    const top = target.getBoundingClientRect().top + window.scrollY - wantedTop;
                    window.scrollTo({ top: Math.max(0, top), behavior: 'instant' });
                } else {
                    const pr = parent.getBoundingClientRect();
                    const tr = target.getBoundingClientRect();
                    parent.scrollTop += (tr.top - pr.top) - Math.max(40, parent.clientHeight * 0.48);
                }
            }
            target.scrollIntoView({ block:'center', inline:'center', behavior:'instant' });
        }
        if (!userSafeVisible(target)) {
            try {
                document.documentElement.style.zoom = '0.9';
                document.body.style.zoom = '0.9';
            } catch (e) {}
            target.scrollIntoView({ block:'center', inline:'center', behavior:'instant' });
            for (const parent of scrollParents(target)) {
                const meta = viewportMeta();
                const wantedTop = meta.safeTop + (meta.safeBottom - meta.safeTop) * 0.48;
                if (parent === document.scrollingElement || parent === document.documentElement || parent === document.body) {
                    const top = target.getBoundingClientRect().top + window.scrollY - wantedTop;
                    window.scrollTo({ top: Math.max(0, top), behavior: 'instant' });
                } else {
                    const pr = parent.getBoundingClientRect();
                    const tr = target.getBoundingClientRect();
                    parent.scrollTop += (tr.top - pr.top) - Math.max(40, parent.clientHeight * 0.48);
                }
            }
            target.scrollIntoView({ block:'center', inline:'center', behavior:'instant' });
        }

        const oldStyle = document.getElementById('agentflowPublishGuideStyle');
        if (oldStyle) oldStyle.remove();
        target.setAttribute('data-agentflow-publish', 'true');
        target.style.outline = '4px solid #ff2d55';
        target.style.boxShadow = '0 0 0 8px rgba(255,45,85,.25)';
        target.style.borderRadius = '8px';
        target.style.transition = 'box-shadow .2s ease, outline .2s ease';

        const style = document.createElement('style');
        style.id = 'agentflowPublishGuideStyle';
        style.textContent = '[data-agentflow-publish="true"]{animation:agentflowPublishPulse 1.1s ease-in-out infinite!important;}@keyframes agentflowPublishPulse{0%,100%{filter:brightness(1)}50%{filter:brightness(1.18)}}';
        document.head.appendChild(style);

        const oldGuide = document.getElementById('agentflowPublishGuide');
        if (oldGuide) oldGuide.remove();
        const guide = document.createElement('div');
        guide.id = 'agentflowPublishGuide';
        guide.textContent = '发布按钮已高亮，请点击红框“发布”。可能需要手机验证码，请手动完成。';
        guide.style.cssText = [
            'position:fixed',
            'left:0',
            'right:0',
            'bottom:40px',
            'z-index:2147483647',
            'height:34px',
            'box-sizing:border-box',
            'display:flex',
            'align-items:center',
            'padding:0 14px',
            'background:rgba(15,23,42,.94)',
            'color:white',
            'font-size:13px',
            'font-weight:800',
            'white-space:nowrap',
            'overflow:hidden',
            'text-overflow:ellipsis',
            'box-shadow:0 -6px 18px rgba(0,0,0,.16)',
            'font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif',
            'pointer-events:none'
        ].join(';');
        document.body.appendChild(guide);
        const cleanupPublishHints = () => {
            try {
                const guide = document.getElementById('agentflowPublishGuide');
                if (guide) guide.remove();
                const overlay = document.getElementById('agentflowRpaOverlay');
                if (overlay) overlay.remove();
                const style = document.getElementById('agentflowPublishGuideStyle');
                if (style) style.remove();
            } catch (e) {}
        };
        target.addEventListener('click', () => setTimeout(cleanupPublishHints, 120), { once:true, capture:true });
        if (typeof window.__agentflowRefreshRpaOverlay === 'function') {
            try { window.__agentflowRefreshRpaOverlay(); } catch (e) {}
        }

        const root = target.closest('form,[class*="publish"],[class*="content"],[class*="form"],main,section') || document.body;
        if (window.__agentflowPublishGuard && window.__agentflowPublishGuard.observer) {
            try { window.__agentflowPublishGuard.observer.disconnect(); } catch (e) {}
        }
        window.__agentflowPublishGuard = { lost:false, rebuilds:0, startedAt:Date.now(), observer:null };
        window.__agentflowPublishGuard.observer = new MutationObserver(() => {
            const marked = document.querySelector('[data-agentflow-publish="true"]');
            if (!marked || !document.body.contains(marked)) window.__agentflowPublishGuard.lost = true;
        });
        window.__agentflowPublishGuard.observer.observe(root, { childList:true, subtree:true });

        return {
            found:true,
            visible:userSafeVisible(target),
            jsVisible:inViewport(target),
            userSafeVisible:userSafeVisible(target),
            text:textOf(target),
            disabled:!!target.disabled || target.getAttribute('aria-disabled') === 'true',
            rect:rectData(target),
            actionBarCount:actionContainers.length,
            buttonCount:buttons.length,
            viewport:viewportMeta(),
        };
    }''')


def guard_and_verify_publish_button(page, task_id: str, timeout_seconds: int = 15) -> bool:
    stable_count = 0
    rebuilds = 0
    max_rebuilds = 3
    rounds = int(timeout_seconds * 2)
    for i in range(rounds):
        time.sleep(0.5)
        try:
            state = page.evaluate('''() => {
                const el = document.querySelector('[data-agentflow-publish="true"]');
                const guard = window.__agentflowPublishGuard || {};
                if (!el || !document.body.contains(el)) return { ok:false, lost:true, reason:'button marker missing' };
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                const innerHeight = window.innerHeight || document.documentElement.clientHeight || 740;
                const innerWidth = window.innerWidth || document.documentElement.clientWidth || 1440;
                const safeTop = Math.min(140, Math.max(84, innerHeight * 0.16));
                const safeBottom = innerHeight - Math.min(180, Math.max(128, innerHeight * 0.22));
                const visible = r.width > 0 && r.height > 0
                    && r.top >= safeTop && r.left >= 0
                    && r.bottom <= safeBottom
                    && r.right <= innerWidth
                    && s.display !== 'none' && s.visibility !== 'hidden';
                const highlighted = (el.style.outline || '').includes('ff2d55') || (el.style.boxShadow || '').includes('255, 45, 85');
                return {
                    ok:visible && highlighted && !guard.lost,
                    lost:!!guard.lost,
                    visible,
                    highlighted,
                    rect:{top:r.top,left:r.left,bottom:r.bottom,right:r.right,width:r.width,height:r.height},
                    viewport:{innerWidth,innerHeight,safeTop,safeBottom,screenY:window.screenY,outerHeight:window.outerHeight,screenAvailHeight:window.screen && window.screen.availHeight || 0}
                };
            }''')
            if state.get('ok'):
                stable_count += 1
                if stable_count >= 10:
                    write_status(task_id, 'publish_button_guard_stable',
                                 message='发布按钮高亮已稳定保持 5 秒。',
                                 guard=state)
                    show_rpa_overlay(
                        page,
                        '发布按钮高亮已稳定',
                        '现在可以接管鼠标，点击红框高亮的真实“发布”按钮；如需手机验证码请手动完成。',
                        phase='ready',
                    )
                    return True
                continue

            stable_count = 0
            if state.get('lost') or not state.get('visible') or not state.get('highlighted'):
                rebuilds += 1
                if rebuilds > max_rebuilds:
                    write_status(task_id, 'publish_button_user_not_visible',
                                 message='发布按钮无法稳定保持在用户安全可见区域。',
                                 guard=state,
                                 rebuilds=rebuilds,
                                 metrics=get_user_window_metrics(page))
                    return False
                found = _locate_publish_button_once(page)
                write_status(task_id, 'publish_button_guard_rebuild',
                             message='发布按钮高亮被页面覆盖，已重新定位并高亮。',
                             rebuilds=rebuilds,
                             locator_result=found)
                show_rpa_overlay(
                    page,
                    '发布按钮被页面刷新，正在重找',
                    f'抖音页面重绘覆盖了高亮，系统正在第 {rebuilds} 次重新定位发布按钮。',
                    phase='warn',
                )
                if not found or not found.get('visible'):
                    return False
        except Exception as e:
            stable_count = 0
            rebuilds += 1
            if rebuilds > max_rebuilds:
                write_status(task_id, 'publish_button_unstable',
                             message='发布按钮守护检测异常。',
                             error=str(e)[:300])
                return False
    return False


def _save_publish_button_debug(page, task_id: str, suffix: str):
    try:
        debug_dir = Path(__file__).parent / 'publish_debug'
        debug_dir.mkdir(exist_ok=True)
        page.screenshot(path=str(debug_dir / f'{task_id}_{suffix}.png'))
    except Exception:
        pass


def _fill_first_visible(page, selectors, value, exclude_placeholders=None, delay=20) -> bool:
    exclude_placeholders = exclude_placeholders or []
    for sel in selectors:
        loc = page.locator(sel)
        count = loc.count()
        for i in range(min(count, 20)):
            el = loc.nth(i)
            try:
                placeholder = el.get_attribute('placeholder') or ''
                if any(word in placeholder for word in exclude_placeholders):
                    continue
                if not el.is_visible():
                    continue
                el.click()
                try:
                    el.fill('')
                    el.type(value, delay=delay)
                except Exception:
                    page.keyboard.press('Meta+A')
                    page.keyboard.press('Backspace')
                    page.keyboard.type(value, delay=delay)
                return True
            except Exception:
                continue
    return False


def _append_first_visible(page, selectors, value, delay=20) -> bool:
    for sel in selectors:
        loc = page.locator(sel)
        count = loc.count()
        for i in range(min(count, 20)):
            el = loc.nth(i)
            try:
                if not el.is_visible():
                    continue
                el.click()
                page.keyboard.press('End')
                el.type(value, delay=delay)
                return True
            except Exception:
                continue
    return False


def main():
    parser = argparse.ArgumentParser(description='抖音创作者中心半自动发布器')
    parser.add_argument('--task-id', required=True, help='任务 ID')
    parser.add_argument('--video-path', required=True, help='视频文件绝对路径')
    parser.add_argument('--title', default='', help='视频标题')
    parser.add_argument('--body', default='', help='视频描述')
    parser.add_argument('--hashtags', default='', help='话题标签，逗号分隔')
    args = parser.parse_args()

    task_id = args.task_id
    video_path = args.video_path
    title = args.title
    body = args.body

    # 参数校验
    if not os.path.exists(video_path):
        write_status(task_id, 'failed',
                     error=f'视频文件不存在: {video_path}')
        sys.exit(1)

    if not title and not body:
        write_status(task_id, 'filled_empty',
                     message='标题和正文均为空，仅上传视频。'
                             '如需在服务端自动填写，请在前端编辑后重新发布。')

    write_status(task_id, 'starting', video=video_path, title=title)

    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        write_status(task_id, 'launching_browser')

        # 持久化上下文：优先复用登录态；如果旧窗口占用 profile，则切换到本次任务独立 profile。
        try:
            context = launch_publish_context(p, task_id)
        except Exception:
            sys.exit(1)

        page = context.new_page()
        page.set_default_timeout(15000)
        prepare_browser_window(page, task_id)
        dismiss_browser_restore_hint(page, task_id)

        exit_code = 0
        try:
            # ===== 阶段 1：导航到上传页 =====
            write_status(task_id, 'navigating')
            ok = navigate_to_upload(page, task_id)
            if not ok:
                exit_code = 1
                context.close()
                sys.exit(exit_code)
            dismiss_browser_restore_hint(page, task_id)

            # ===== 阶段 2：检查登录状态 =====
            if not is_logged_in(page):
                logged = wait_for_login(page, task_id, timeout_seconds=120)
                if not logged:
                    context.close()
                    sys.exit(1)

                # 登录成功，重新导航到上传页
                ok = navigate_to_upload(page, task_id)
                if not ok:
                    context.close()
                    sys.exit(1)
                dismiss_browser_restore_hint(page, task_id)

            # ===== 阶段 2.5：关闭遮挡弹窗 =====
            dismiss_overlays(page, task_id)

            # ===== 阶段 3：上传视频 =====
            # 上传前截图，便于调试
            try:
                debug_dir = Path(__file__).parent / 'publish_debug'
                debug_dir.mkdir(exist_ok=True)
                page.screenshot(path=str(debug_dir / f'{task_id}_before_upload.png'))
            except Exception:
                pass

            ok = upload_video(page, video_path, task_id)
            if not ok:
                context.close()
                sys.exit(1)

            # ===== 阶段 4：等待转码完成 =====
            ok = wait_for_upload_complete(page, task_id, timeout_seconds=600)
            if not ok:
                context.close()
                sys.exit(1)

            # ===== 阶段 4.2：等待抖音从上传页跳转到发布编辑页 =====
            ok = wait_for_post_video_page(page, task_id, timeout_seconds=180)
            if not ok:
                context.close()
                sys.exit(1)

            # ===== 阶段 4.5：上传后二次清理弹窗（"视频预览功能"提示框等）=====
            dismiss_overlays(page, task_id)
            wait_for_publish_page_stable(page, task_id, timeout_seconds=10)

            # ===== 阶段 5：填写标题和描述 =====
            fill_title_and_body(page, title, body, args.hashtags, task_id)
            wait_for_publish_page_stable(page, task_id, timeout_seconds=10)

            # ===== 阶段 6：滚动到发布按钮并高亮 =====
            publish_button_found = locate_publish_button(page, task_id)
            if not publish_button_found:
                print('\n========================================')
                print('草稿已填写，但发布按钮没有进入当前视口。')
                print('请保留浏览器窗口，调试截图已写入 publish_debug。')
                print('========================================\n')
                while True:
                    try:
                        _ = page.url
                        time.sleep(2)
                    except Exception:
                        break
                context.close()
                sys.exit(1)

            # ===== 完成：停在发布按钮前 =====
            ready_message = '视频已上传，标题/描述已填写。请点击已高亮的"发布"按钮。'
            write_status(task_id, 'draft_ready',
                         message=ready_message,
                         title_filled=bool(title),
                         body_filled=bool(body),
                         publish_button_found=True,
                         hashtags_count=len(args.hashtags.split(',')) if args.hashtags else 0)

            # 保持浏览器打开，等待用户手动发布
            print('\n========================================')
            print('草稿已就绪，请在浏览器窗口中检查并手动发布。')
            print('发布完成后关闭浏览器窗口即可。')
            print('========================================\n')

            # 等待用户关闭浏览器
            while True:
                try:
                    current_url = page.url
                    if 'creator-micro/content/manage' in current_url:
                        cleanup_publish_overlays(page)
                    time.sleep(2)
                except Exception:
                    break

            write_status(task_id, 'completed',
                         message='浏览器窗口已关闭，发布流程结束。'
                                 '如已手动点击发布，视频将进入抖音审核。')

        except Exception as e:
            write_status(task_id, 'failed', error=str(e))
            exit_code = 1
        finally:
            try:
                context.close()
            except Exception:
                pass

    sys.exit(exit_code)


if __name__ == '__main__':
    main()
