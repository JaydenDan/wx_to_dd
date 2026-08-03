import asyncio
import os
import sys
import time
import re
from typing import Optional, Dict, Any
from urllib.parse import urlparse

# 将项目根目录添加到 sys.path，解决模块导入问题
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.config.global_config import PLAYWRIGHT_HEADLESS, PLATFORM_CONFIG, SCREENSHOT_DELAY
from playwright.async_api import async_playwright, Page, Browser, Playwright, BrowserContext
from loguru import logger

# 全局变量存储 Playwright 和 Browser 实例
_GLOBAL_PLAYWRIGHT: Optional[Playwright] = None
# 仅供抖音/小红书游客任务复用的 Chromium 进程。每个任务仍使用独立 Context。
_GLOBAL_GUEST_BROWSER: Optional[Browser] = None
# 用于仍依赖登录态的平台的持久化 BrowserContext。
_GLOBAL_CONTEXT: Optional[BrowserContext] = None
# 全局信号量控制并发。
_GLOBAL_SEMAPHORE: Optional[asyncio.Semaphore] = None
# 将启动、关闭和重建串行化，避免多条消息同时重建同一浏览器资源。
_GLOBAL_LIFECYCLE_LOCK: Optional[asyncio.Lock] = None

BROWSER_ARGS = [
    '--no-sandbox',
    '--disable-setuid-sandbox',
    '--disable-blink-features=AutomationControlled',
]
BROWSER_VIEWPORT = {'width': 1920, 'height': 1080}
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
WEBDRIVER_HIDE_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', {
        get: () => undefined
    });
"""

GUEST_PLATFORM_COOKIE_DOMAINS = re.compile(
    r"(^|\.)(douyin\.com|iesdouyin\.com|xiaohongshu\.com|xhslink\.com|xhslink\.cn)$",
    re.IGNORECASE,
)

LOGIN_OVERLAY_GUARD_SCRIPT = r"""
(() => {
    const installGuard = () => {
        if (window.__wxToDdLoginOverlayGuardInstalled) return;
        if (!document.documentElement) {
            setTimeout(installGuard, 0);
            return;
        }

        const hostname = location.hostname.toLowerCase();
        const isDouyin = /(^|\.)douyin\.com$/.test(hostname) ||
            /(^|\.)iesdouyin\.com$/.test(hostname);
        const isXhs = /(^|\.)xiaohongshu\.com$/.test(hostname) ||
            /(^|\.)xhslink\.com$/.test(hostname) ||
            /(^|\.)xhslink\.cn$/.test(hostname);
        if (!isDouyin && !isXhs) return;

        window.__wxToDdLoginOverlayGuardInstalled = true;
        window.__wxToDdLoginOverlayHandledCount = 0;
        window.__wxToDdLoginOverlayReportedCount = 0;

        const ensureBlockingStyle = () => {
            if (document.getElementById('wx-to-dd-login-overlay-guard')) return;
            const style = document.createElement('style');
            style.id = 'wx-to-dd-login-overlay-guard';
            style.textContent = `
                [id^="login-full-panel-"] {
                    display: none !important;
                    visibility: hidden !important;
                    pointer-events: none !important;
                }
                .reds-modal.login-modal, .login-modal {
                    display: none !important;
                    visibility: hidden !important;
                    pointer-events: none !important;
                }
                html:has(.login-modal) .reds-mask {
                    display: none !important;
                    pointer-events: none !important;
                }
            `;
            (document.head || document.documentElement).appendChild(style);
        };

        const clickClose = (container, selector) => {
            const closeIcon = container.querySelector(selector);
            if (!closeIcon) return;
            const clickTarget = closeIcon.closest('button, [role="button"], div') || closeIcon;
            try {
                clickTarget.click();
            } catch (_) {
                // DOM removal below remains the fallback.
            }
        };

        const closeLoginOverlays = () => {
            let handled = 0;

            if (isDouyin) {
                const panels = [...document.querySelectorAll('[id^="login-full-panel-"]')]
                    .filter(panel =>
                        panel.querySelector('#douyin-login-new-id, #login-panel-new') ||
                        (panel.textContent || '').includes('登录后')
                    );
                for (const panel of panels) {
                    const portal = panel.closest('.semi-portal');
                    clickClose(
                        panel,
                        'svg[width="37"][height="36"], svg[width="36"][height="36"], [aria-label*="关闭"]'
                    );
                    const target = portal || panel;
                    if (target.isConnected) target.remove();
                    handled += 1;
                }
            }

            if (isXhs) {
                const modals = [...document.querySelectorAll('.reds-modal.login-modal, .login-modal')];
                for (const modal of modals) {
                    clickClose(modal, '.close-button, [aria-label*="关闭"]');
                    if (modal.isConnected) modal.remove();
                    handled += 1;
                }
                if (handled > 0) {
                    document.querySelectorAll('.reds-mask[aria-label="弹窗遮罩"], .reds-mask')
                        .forEach(mask => mask.remove());
                    document.documentElement.classList.remove('reds-lock-scroll');
                    document.body?.classList.remove('reds-lock-scroll');
                    if (document.documentElement.style.overflow === 'hidden') {
                        document.documentElement.style.removeProperty('overflow');
                    }
                    if (document.body?.style.overflow === 'hidden') {
                        document.body.style.removeProperty('overflow');
                    }
                }
            }

            window.__wxToDdLoginOverlayHandledCount += handled;
            return handled;
        };

        window.__wxToDdCloseLoginOverlays = closeLoginOverlays;
        window.__wxToDdConsumeLoginOverlayCount = () => {
            closeLoginOverlays();
            const total = window.__wxToDdLoginOverlayHandledCount;
            const delta = total - window.__wxToDdLoginOverlayReportedCount;
            window.__wxToDdLoginOverlayReportedCount = total;
            return delta;
        };

        let scheduled = false;
        const scheduleClose = () => {
            if (scheduled) return;
            scheduled = true;
            queueMicrotask(() => {
                scheduled = false;
                ensureBlockingStyle();
                closeLoginOverlays();
            });
        };

        ensureBlockingStyle();
        closeLoginOverlays();
        const observer = new MutationObserver(scheduleClose);
        observer.observe(document.documentElement, {childList: true, subtree: true});
    };

    installGuard();
})();
"""


def _guest_platform_from_url(url: str) -> Optional[str]:
    """根据输入链接判断是否需要游客隔离 Context。"""
    try:
        hostname = (urlparse(url).hostname or "").lower()
    except ValueError:
        return None

    if not hostname:
        return None
    if hostname == "douyin.com" or hostname.endswith(".douyin.com"):
        return "douyin"
    if hostname == "iesdouyin.com" or hostname.endswith(".iesdouyin.com"):
        return "douyin"
    if hostname == "xiaohongshu.com" or hostname.endswith(".xiaohongshu.com"):
        return "xhs"
    if hostname == "xhslink.com" or hostname.endswith(".xhslink.com"):
        return "xhs"
    if hostname == "xhslink.cn" or hostname.endswith(".xhslink.cn"):
        return "xhs"
    return None


def _is_target_closed_error(error: BaseException) -> bool:
    """仅识别 Playwright 资源被关闭，避免把普通网络断开误判为浏览器崩溃。"""
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "target page, context or browser has been closed",
            "target closed",
            "browser has been closed",
            "context has been closed",
            "connection closed while reading from the driver",
        )
    )

class PlaywrightManager:
    """
    全局 Playwright 管理器，实现浏览器复用，支持高并发。
    """
    # 用户数据目录，用于保存登录状态
    USER_DATA_DIR = os.path.join(os.getcwd(), "user_data")

    # 新增：记录当前是否为无头模式
    _CURRENT_HEADLESS: bool = PLAYWRIGHT_HEADLESS

    @staticmethod
    def _get_lifecycle_lock() -> asyncio.Lock:
        global _GLOBAL_LIFECYCLE_LOCK
        if _GLOBAL_LIFECYCLE_LOCK is None:
            _GLOBAL_LIFECYCLE_LOCK = asyncio.Lock()
        return _GLOBAL_LIFECYCLE_LOCK

    @staticmethod
    def _guest_browser_is_connected() -> bool:
        if _GLOBAL_GUEST_BROWSER is None:
            return False
        try:
            return _GLOBAL_GUEST_BROWSER.is_connected()
        except Exception:
            return False

    @staticmethod
    async def _ensure_playwright_locked() -> None:
        global _GLOBAL_PLAYWRIGHT
        if _GLOBAL_PLAYWRIGHT is None:
            logger.info("[lifecycle] 启动 Playwright 驱动...")
            _GLOBAL_PLAYWRIGHT = await async_playwright().start()

    @staticmethod
    async def _create_persistent_context_locked() -> BrowserContext:
        global _GLOBAL_CONTEXT
        if _GLOBAL_PLAYWRIGHT is None:
            raise RuntimeError("Playwright 驱动未启动")

        if not os.path.exists(PlaywrightManager.USER_DATA_DIR):
            os.makedirs(PlaywrightManager.USER_DATA_DIR)

        context = await _GLOBAL_PLAYWRIGHT.chromium.launch_persistent_context(
            user_data_dir=PlaywrightManager.USER_DATA_DIR,
            headless=PlaywrightManager._CURRENT_HEADLESS,
            args=BROWSER_ARGS,
            ignore_default_args=["--enable-automation"],
            viewport=BROWSER_VIEWPORT,
            user_agent=BROWSER_USER_AGENT,
        )
        try:
            await context.add_init_script(WEBDRIVER_HIDE_SCRIPT)
            await context.add_init_script(LOGIN_OVERLAY_GUARD_SCRIPT)
            await context.clear_cookies(domain=GUEST_PLATFORM_COOKIE_DOMAINS)
            context.set_default_timeout(30000)
        except Exception:
            try:
                await context.close()
            except Exception:
                pass
            raise

        _GLOBAL_CONTEXT = context
        logger.info("[persistent] 登录 Context 已启动。")
        logger.info("抖音和小红书已启用游客模式，不加载账号登录态。")
        return context

    @staticmethod
    async def _create_guest_browser_locked() -> Browser:
        global _GLOBAL_GUEST_BROWSER
        if _GLOBAL_PLAYWRIGHT is None:
            raise RuntimeError("Playwright 驱动未启动")

        _GLOBAL_GUEST_BROWSER = await _GLOBAL_PLAYWRIGHT.chromium.launch(
            headless=PlaywrightManager._CURRENT_HEADLESS,
            args=BROWSER_ARGS,
            ignore_default_args=["--enable-automation"],
        )
        logger.info("[guest] 游客 Browser 已启动，抖音和小红书任务将各自使用独立 Context。")
        return _GLOBAL_GUEST_BROWSER

    @staticmethod
    async def _close_persistent_context_locked() -> None:
        global _GLOBAL_CONTEXT
        context = _GLOBAL_CONTEXT
        _GLOBAL_CONTEXT = None
        if context is None:
            return
        try:
            await context.close()
        except Exception as exc:
            logger.debug("[persistent] 关闭 Context 时已失联: {}", exc)

    @staticmethod
    async def _close_guest_browser_locked() -> None:
        global _GLOBAL_GUEST_BROWSER
        browser = _GLOBAL_GUEST_BROWSER
        _GLOBAL_GUEST_BROWSER = None
        if browser is None:
            return
        try:
            await browser.close()
        except Exception as exc:
            logger.debug("[guest] 关闭 Browser 时已失联: {}", exc)

    @staticmethod
    async def start(headless: Optional[bool] = None, check_login: bool = True):
        """
        启动 Playwright Browser
        :param headless: 是否使用无头模式
        :param check_login: 是否检查仍需要人工验证的平台；抖音和小红书始终使用游客模式
        """
        global _GLOBAL_CONTEXT, _GLOBAL_SEMAPHORE

        if headless is None:
            headless = PLAYWRIGHT_HEADLESS

        PlaywrightManager._CURRENT_HEADLESS = headless

        should_check_login = False
        async with PlaywrightManager._get_lifecycle_lock():
            if _GLOBAL_SEMAPHORE is None:
                _GLOBAL_SEMAPHORE = asyncio.Semaphore(15)

            persistent_ready = _GLOBAL_CONTEXT is not None
            guest_ready = PlaywrightManager._guest_browser_is_connected()
            if persistent_ready and guest_ready:
                logger.info("Playwright 浏览器环境已就绪，无需重复启动。")
                return

            mode_str = "无头模式 (后台隐身)" if headless else "有头模式 (可见窗口)"
            logger.info("准备启动 Playwright 浏览器环境: {}", mode_str)
            try:
                await PlaywrightManager._ensure_playwright_locked()
                if not persistent_ready:
                    await PlaywrightManager._create_persistent_context_locked()
                    should_check_login = check_login
                if not guest_ready:
                    await PlaywrightManager._close_guest_browser_locked()
                    await PlaywrightManager._create_guest_browser_locked()
            except Exception as exc:
                logger.exception("Playwright 浏览器环境启动失败: {}", exc)
                return

        if should_check_login:
            await PlaywrightManager.check_login_status()

    @staticmethod
    async def ensure_persistent_context() -> Optional[BrowserContext]:
        """获取登录态 Context；缺失时仅重建该 Context，不影响游客 Browser。"""
        global _GLOBAL_CONTEXT
        async with PlaywrightManager._get_lifecycle_lock():
            if _GLOBAL_CONTEXT is not None:
                return _GLOBAL_CONTEXT
            logger.warning("[persistent] Context 未初始化，开始串行创建。")
            try:
                await PlaywrightManager._ensure_playwright_locked()
                return await PlaywrightManager._create_persistent_context_locked()
            except Exception as exc:
                logger.exception("[persistent] Context 创建失败: {}", exc)
                return None

    @staticmethod
    async def ensure_guest_browser() -> Optional[Browser]:
        """获取游客 Browser；缺失时串行创建，避免多条消息同时启动 Chromium。"""
        async with PlaywrightManager._get_lifecycle_lock():
            if PlaywrightManager._guest_browser_is_connected():
                return _GLOBAL_GUEST_BROWSER
            if _GLOBAL_GUEST_BROWSER is None:
                logger.warning("[guest] Browser 未初始化，开始串行创建。")
            else:
                logger.warning("[guest] Browser 已断开，开始串行重建。")
            try:
                await PlaywrightManager._close_guest_browser_locked()
                await PlaywrightManager._ensure_playwright_locked()
                return await PlaywrightManager._create_guest_browser_locked()
            except Exception as exc:
                logger.exception("[guest] Browser 创建失败: {}", exc)
                return None

    @staticmethod
    async def create_guest_context(
        browser: Browser,
        platform: str,
        url: str,
    ) -> BrowserContext:
        """为单条抖音/小红书消息创建隔离 Context，不共享 Cookie、页面或关闭事件。"""
        logger.info("[guest] 创建独立 Context: {} {}", platform, url)
        context = None
        try:
            context = await browser.new_context(
                viewport=BROWSER_VIEWPORT,
                user_agent=BROWSER_USER_AGENT,
            )
            await context.add_init_script(WEBDRIVER_HIDE_SCRIPT)
            await context.add_init_script(LOGIN_OVERLAY_GUARD_SCRIPT)
            context.set_default_timeout(30000)
            return context
        except Exception:
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
            raise

    @staticmethod
    async def restart_guest_browser(expected_browser: Optional[Browser]) -> None:
        """只重建失联的游客 Browser；已被其他任务重建时直接复用。"""
        async with PlaywrightManager._get_lifecycle_lock():
            if (
                expected_browser is not None
                and _GLOBAL_GUEST_BROWSER is not None
                and _GLOBAL_GUEST_BROWSER is not expected_browser
                and PlaywrightManager._guest_browser_is_connected()
            ):
                logger.info("[guest] Browser 已由其他任务重建，复用新实例。")
                return

            logger.warning("[guest] Browser 已断开，开始串行重建。")
            await PlaywrightManager._close_guest_browser_locked()
            await PlaywrightManager._ensure_playwright_locked()
            await PlaywrightManager._create_guest_browser_locked()
            logger.info("[guest] Browser 串行重建完成。")

    @staticmethod
    async def restart_persistent_context(expected_context: Optional[BrowserContext]) -> None:
        """只重建失联的登录态 Context；不关闭游客 Browser。"""
        async with PlaywrightManager._get_lifecycle_lock():
            if (
                expected_context is not None
                and _GLOBAL_CONTEXT is not None
                and _GLOBAL_CONTEXT is not expected_context
            ):
                logger.info("[persistent] Context 已由其他任务重建，复用新实例。")
                return

            logger.warning("[persistent] Context 已断开，开始串行重建。")
            await PlaywrightManager._close_persistent_context_locked()
            await PlaywrightManager._ensure_playwright_locked()
            await PlaywrightManager._create_persistent_context_locked()
            logger.info("[persistent] Context 串行重建完成。")

    @staticmethod
    async def check_login_status():
        """
        检查仍需要人工验证的平台。抖音和小红书不参与登录检查。
        """
        global _GLOBAL_CONTEXT
        if not _GLOBAL_CONTEXT:
            return

        platforms_to_check = {
            "kuaishou": "https://c.kuaishou.com/fw/photo/3xu8bwer63hrre2"
        }
        
        page = await _GLOBAL_CONTEXT.new_page()
        try:
            for name, url in platforms_to_check.items():
                logger.info("检查 [{}] 登录状态...", name)
                await page.goto(url, wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)
                
                is_logged_in = False
                
                # 仅对仍需要人工验证的平台执行登录判断。
                if name == "kuaishou":
                    # 快手主要检查是否有滑块验证码
                    try:
                        # 延迟1秒检测，等待验证码加载
                        await page.wait_for_timeout(1000)
                        
                        has_captcha = False
                        # 1. 主页面检查
                        if await page.locator("text='向右拖动滑块填充拼图'").count() > 0:
                            has_captcha = True
                        
                        # 2. iframe 检查 (如果主页面没找到)
                        if not has_captcha:
                            for frame in page.frames:
                                try:
                                    if await frame.locator("text='向右拖动滑块填充拼图'").count() > 0:
                                        has_captcha = True
                                        break
                                except:
                                    pass

                        # 检查是否存在滑块验证码文本
                        if has_captcha:
                            is_logged_in = False
                            logger.warning("[kuaishou] 检测到滑块验证码！")
                        else:
                            # 简单的反向检查：如果没有验证码，就算已就绪
                            # 关键修改：不能只靠验证码消失来判断登录，因为滑块验证成功后可能只是验证码消失了，但还没登录
                            # 快手验证成功后，通常会留在当前页或者刷新，需要检查是否有登录后的特征
                            # 例如头像：.avatar-img 或者 .header-user-avatar
                            if await page.locator(".avatar-img, .header-user-avatar, .user-avatar").count() > 0:
                                is_logged_in = True
                            # 或者检查是否有“登录”按钮，如果没有了也算
                            elif await page.locator("text='登录'").count() == 0:
                                # 但要注意，有时候未登录也没有显式的登录按钮，或者在折叠菜单里
                                # 所以最好结合 Cookie 检查
                                cookies = await _GLOBAL_CONTEXT.cookies(url)
                                for cookie in cookies:
                                    if cookie['name'] == 'kuaishou.server.web_st': # 示例 Cookie
                                        is_logged_in = True
                                        break
                                if not is_logged_in:
                                     # 如果实在找不到特征，且没有验证码，暂时认为已登录（因为我们主要是为了过验证码）
                                     # 只要滑块过了，后续的截图就能正常进行了，不需要完全登录账号
                                     is_logged_in = True
                            else:
                                is_logged_in = False

                    except:
                        # 报错通常意味着没找到元素，可能就是正常的
                        is_logged_in = True

                if is_logged_in:
                    logger.info("[{}] 已检测到登录状态。", name)
                else:
                    logger.warning("[{}] 未检测到登录状态！准备进行手动登录...", name)
                    await PlaywrightManager.perform_manual_login(name, url)
                    # 登录完一个后，需要重新获取 page，因为 perform_manual_login 可能会重启浏览器或者关闭页面
                    # 如果重启了浏览器，context 会变，上面的 page 对象就失效了
                    # 检查 context 是否还在
                    if not _GLOBAL_CONTEXT:
                        logger.warning("Context 已失效，停止后续检查。")
                        break
                    
                    # 即使 Context 还在，原来的 page 也可能因为 perform_manual_login 里的操作被关闭了或者状态不对
                    # 最好是重新 new_page
                    try:
                        await page.close()
                    except:
                        pass
                    
                    # 重新创建页面继续下一个检查
                    page = await _GLOBAL_CONTEXT.new_page()
                    
        except Exception as e:
            # 忽略目标页面关闭导致的错误，因为在 check_login_status 中我们可能只是想检查一下，
            # 如果页面在检查过程中被关闭了（比如用户手动关闭了），这不算严重错误。
            if "Target page, context or browser has been closed" in str(e):
                logger.warning("检查登录状态时页面被关闭: {}", e)
            else:
                logger.error("检查登录状态出错: {}", e)
        finally:
            # 确保页面关闭，使用 try-except 包裹防止 double close
            if page:
                try:
                    await page.close()
                except:
                    pass

    @staticmethod
    async def perform_manual_login(platform_name: str, url: str):
        """
        执行手动登录流程：关闭当前无头模式，重启为有头模式，等待用户登录
        """
        global _GLOBAL_CONTEXT, _GLOBAL_PLAYWRIGHT

        if platform_name in {"douyin", "xhs"}:
            logger.info("[{}] 已配置为游客模式，跳过人工登录。", platform_name)
            return
        
        # 检查当前是否已经是 有头模式
        is_headless = PlaywrightManager._CURRENT_HEADLESS
        
        # 记录用于登录的页面，避免重复打开
        login_page = None

        if is_headless:
            logger.info("当前为无头模式，正在重启浏览器以进行 [{}] 手动登录...", platform_name)
            # 关闭当前实例
            await PlaywrightManager.stop()
            # 以有头模式重启
            await PlaywrightManager.start(headless=False, check_login=False)
            if _GLOBAL_CONTEXT:
                login_page = await _GLOBAL_CONTEXT.new_page()
                await login_page.goto(url)
        else:
            logger.info("当前已有浏览器窗口，直接打开 [{}] 登录页面...", platform_name)
            # 只有当没有页面时才新建，或者直接在现有页面跳转
            # 但为了不影响其他页面，还是新建一个比较好
            # 为了避免重复弹出，检查当前是否有页面已经在该 url
            if _GLOBAL_CONTEXT:
                pages = _GLOBAL_CONTEXT.pages
                found = False
                for p in pages:
                    if platform_name in p.url:
                        login_page = p
                        await login_page.bring_to_front()
                        found = True
                        break
                if not found:
                    login_page = await _GLOBAL_CONTEXT.new_page()
                    await login_page.goto(url)
        
        if not _GLOBAL_CONTEXT or not login_page:
            logger.error("浏览器环境异常，无法进行登录！")
            return

        logger.info("请在弹出的浏览器窗口中完成 [{}] 的登录...", platform_name)
        
        # 循环检测登录状态，直到检测到登录成功
        # 设置最大等待时间，例如 5 分钟
        max_wait_time = 300 
        start_time = time.time()
        
        while True:
            if time.time() - start_time > max_wait_time:
                logger.error("[{}] 登录超时！", platform_name)
                break
                
            is_logged_in = False
            # 使用 login_page 进行检测，而不是新建页面
            page = login_page 
            
            try:
                # 快手仍使用人工验证；抖音和小红书由游客模式处理。
                if platform_name == "kuaishou":
                     # 快手：只要验证码消失了，就算成功
                     has_captcha = False
                     if await page.locator("text='向右拖动滑块填充拼图'").count() > 0:
                         has_captcha = True
                     
                     if not has_captcha:
                         for frame in page.frames:
                             try:
                                 if await frame.locator("text='向右拖动滑块填充拼图'").count() > 0:
                                     has_captcha = True
                                     break
                             except:
                                 pass

                     if not has_captcha:
                         # 增强检查：如果还有“登录”按钮，说明验证码虽然过了但还没登录
                         # 但如果只是为了截图，其实不需要登录账号，只要验证码过了就行
                         # 用户反馈说手动滑过验证码后直接成功，但进入作品页截图还是验证码
                         # 这说明验证码的 Cookie 可能没存上，或者跳转有问题
                         
                         # 这里我们尝试等待一下，看看页面是否会刷新
                         await page.wait_for_timeout(2000)
                         
                         # 如果还在验证码页面（虽然滑块没了），那也不算成功
                         if "captcha" in page.url:
                             is_logged_in = False
                         else:
                             is_logged_in = True

            except:
                pass

            if is_logged_in:
                logger.info("[{}] 登录成功检测通过！", platform_name)
                break
            
            await asyncio.sleep(1)
            
        await asyncio.sleep(2)
        await login_page.close()
        
        # 只有当原始模式是 headless 时，才需要切换回去
        if is_headless:
            logger.info("登录完成，正在切换回无头模式继续运行...")
            await PlaywrightManager.stop()
            await PlaywrightManager.start(headless=True)
        else:
            logger.info("登录完成，保持有头模式继续运行...")


    @staticmethod
    async def stop():
        global _GLOBAL_PLAYWRIGHT, _GLOBAL_SEMAPHORE
        async with PlaywrightManager._get_lifecycle_lock():
            has_guest_browser = _GLOBAL_GUEST_BROWSER is not None
            await PlaywrightManager._close_guest_browser_locked()
            if has_guest_browser:
                logger.info("[guest] Browser 已关闭。")

            has_persistent_context = _GLOBAL_CONTEXT is not None
            await PlaywrightManager._close_persistent_context_locked()
            if has_persistent_context:
                logger.info("[persistent] Context 已关闭。")

            if _GLOBAL_PLAYWRIGHT:
                playwright = _GLOBAL_PLAYWRIGHT
                _GLOBAL_PLAYWRIGHT = None
                try:
                    await playwright.stop()
                    logger.info("[lifecycle] Playwright 服务已停止。")
                except Exception as exc:
                    logger.debug("[lifecycle] 停止 Playwright 服务时已失联: {}", exc)

            _GLOBAL_SEMAPHORE = None

    @staticmethod
    def get_context() -> Optional[BrowserContext]:
        return _GLOBAL_CONTEXT

    @staticmethod
    def get_semaphore() -> asyncio.Semaphore:
        global _GLOBAL_SEMAPHORE
        if _GLOBAL_SEMAPHORE is None:
             _GLOBAL_SEMAPHORE = asyncio.Semaphore(15)
        return _GLOBAL_SEMAPHORE

class PlaywrightIpChecker:
    """
    使用 Playwright 替代原有基于 requests 的 IP 获取工具。
    支持截图和页面交互。复用全局浏览器实例。
    """
    def __init__(self, screenshot_dir: str = "screenshots"):
        """
        初始化
        :param screenshot_dir: 截图保存目录，默认为项目根目录下的 screenshots
        """
        self.screenshot_dir = screenshot_dir
        if not os.path.exists(self.screenshot_dir):
            try:
                os.makedirs(self.screenshot_dir)
                logger.info("创建截图目录: {}", self.screenshot_dir)
            except Exception as e:
                logger.error("创建截图目录失败: {}", e)

    async def _dismiss_login_overlay(self, page: Page, platform: str) -> int:
        if platform not in {"douyin", "xhs"}:
            return 0

        try:
            await page.evaluate(LOGIN_OVERLAY_GUARD_SCRIPT)
            handled = await page.evaluate(
                "() => window.__wxToDdConsumeLoginOverlayCount?.() || 0"
            )
            if handled:
                logger.info("[{}] 已自动关闭登录弹窗并清理遮罩。", platform)
            return int(handled)
        except Exception as exc:
            if _is_target_closed_error(exc):
                raise
            logger.debug("[{}] 页面跳转期间暂时无法清理登录弹窗: {}", platform, exc)
            return 0

    async def process_any_url(self, url: str, force_screenshot_only: bool = False, use_temp_file: bool = False, retry_count: int = 0) -> Optional[Dict[str, Any]]:
        """
        统一入口：处理任意 URL，自动判断平台并分发
        :param url: 目标 URL
        :param force_screenshot_only: 强制仅截图模式（忽略 IP 获取）
        :param use_temp_file: 是否使用临时文件保存截图（用于发送后即弃的场景）
        :param retry_count: 重试次数（内部使用）
        """
        guest_platform = _guest_platform_from_url(url)
        async with PlaywrightManager.get_semaphore():
            for attempt in range(max(retry_count, 0), 2):
                browser = None
                context = None
                page = None
                try:
                    if guest_platform:
                        browser = await PlaywrightManager.ensure_guest_browser()
                        if browser is None:
                            logger.error("[guest] Browser 不可用，任务终止。URL: {}", url)
                            return None

                        context = await PlaywrightManager.create_guest_context(
                            browser,
                            guest_platform,
                            url,
                        )
                    else:
                        context = await PlaywrightManager.ensure_persistent_context()
                        if context is None:
                            logger.error("[persistent] Context 不可用，任务终止。URL: {}", url)
                            return None
                        logger.debug("[persistent] 复用登录 Context: {}", url)

                    page = await context.new_page()
                    if guest_platform:
                        logger.info("[guest] Context 页面已创建: {} {}", guest_platform, url)
                    else:
                        logger.debug("[persistent] Context 页面已创建: {}", url)
                    return await self._process_page(
                        page,
                        url,
                        force_screenshot_only,
                        use_temp_file,
                    )
                except Exception as exc:
                    if _is_target_closed_error(exc) and attempt < 1:
                        if guest_platform:
                            if PlaywrightManager._guest_browser_is_connected():
                                logger.warning(
                                    "[guest] 独立 Context 已关闭，保留 Browser 后准备第 {}/2 次处理。URL: {}",
                                    attempt + 2,
                                    url,
                                )
                            else:
                                logger.warning(
                                    "[guest] Browser 已断开，重建后准备第 {}/2 次处理。URL: {}",
                                    attempt + 2,
                                    url,
                                )
                                try:
                                    await PlaywrightManager.restart_guest_browser(browser)
                                except Exception as restart_exc:
                                    logger.exception("[guest] Browser 重建失败，任务终止: {}", restart_exc)
                                    return None
                        else:
                            logger.warning(
                                "[persistent] Context 已断开，重建后准备第 {}/2 次处理。URL: {}",
                                attempt + 2,
                                url,
                            )
                            try:
                                await PlaywrightManager.restart_persistent_context(context)
                            except Exception as restart_exc:
                                logger.exception("[persistent] Context 重建失败，任务终止: {}", restart_exc)
                                return None
                        continue

                    logger.exception("任务处理异常: {}", exc)
                    return None
                finally:
                    if page is not None:
                        try:
                            await page.close()
                        except Exception as close_exc:
                            logger.debug("页面关闭时已失联: {}", close_exc)
                    if guest_platform and context is not None:
                        logger.info("[guest] 关闭独立 Context: {} {}", guest_platform, url)
                        try:
                            await context.close()
                            logger.info("[guest] 独立 Context 已关闭: {} {}", guest_platform, url)
                        except Exception as close_exc:
                            logger.debug("[guest] 关闭独立 Context 时已失联: {}", close_exc)

        return None

    async def _process_page(
        self,
        page: Page,
        url: str,
        force_screenshot_only: bool,
        use_temp_file: bool,
    ) -> Dict[str, Any]:
        logger.info("正在访问: {} (Force Screenshot: {})", url, force_screenshot_only)

        # 1. 访问 URL
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1000)
        except Exception as exc:
            if _is_target_closed_error(exc):
                raise
            logger.warning("页面加载可能不完整: {}", exc)

        # 2. 获取最终跳转后的 URL 和域名
        final_url = page.url
        logger.debug("最终 URL: {}", final_url)

        platform = "unknown"

        # 3. 域名匹配与策略分发
        requires_ip = False
        is_mobile = False

        if "mobile_screen" in PLATFORM_CONFIG:
            for domain in PLATFORM_CONFIG["mobile_screen"]:
                if domain in final_url:
                    is_mobile = True
                    break

        if is_mobile:
            logger.info("[{}] 切换至移动端视口 (430x932)", platform)
            await page.set_viewport_size({"width": 430, "height": 932})

        if not force_screenshot_only:
            for domain in PLATFORM_CONFIG["ip_required"]:
                if domain in final_url:
                    if "xiaohongshu" in domain or "xhslink" in domain:
                        platform = "xhs"
                    elif "douyin" in domain:
                        platform = "douyin"
                    elif "weibo" in domain:
                        platform = "weibo"
                    requires_ip = True
                    break
        else:
            logger.debug("已启用强制截图模式，跳过 IP 获取逻辑。")

        if platform == "unknown":
            for domain in PLATFORM_CONFIG["screenshot_only"]:
                if domain in final_url:
                    platform = domain.split(".")[0]
                    break

            if platform == "unknown":
                for domain in PLATFORM_CONFIG["ip_required"]:
                    if domain in final_url:
                        if "xiaohongshu" in domain or "xhslink" in domain:
                            platform = "xhs"
                        elif "douyin" in domain:
                            platform = "douyin"
                        elif "weibo" in domain:
                            platform = "weibo"
                        break

        if platform == "unknown":
            logger.debug("未知平台链接，将仅执行截图: {}", final_url)

        # 4. 抖音和小红书游客模式：关闭登录弹窗并清理遮罩。
        if platform in {"douyin", "xhs"}:
            await self._dismiss_login_overlay(page, platform)

        # 4.5 等待加载
        if SCREENSHOT_DELAY > 0:
            logger.info("[{}] 截图前额外等待 {} 秒...", platform, SCREENSHOT_DELAY)
            await asyncio.sleep(SCREENSHOT_DELAY)

        # 5. 截图
        if use_temp_file:
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                filepath = tmp.name
            logger.info("[{}] 使用临时截图文件: {}", platform, filepath)
        else:
            import uuid
            filename = f"{platform}_{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
            filepath = os.path.join(self.screenshot_dir, filename)

        try:
            await page.screenshot(path=filepath, full_page=False)
            if not use_temp_file:
                logger.info("[{}] 截图已保存: {}", platform, filepath)
        except Exception as exc:
            if _is_target_closed_error(exc):
                raise
            logger.error("[{}] 截图失败: {}", platform, exc)
            if use_temp_file and os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except Exception:
                    pass
            filepath = None

        # 6. 提取 IP (如果需要)
        true_address = None
        if requires_ip:
            true_address = await self._extract_ip(page, platform, final_url)
        else:
            logger.info("[{}] 无需提取 IP。", platform)

        return {
            "true_address": true_address,
            "screenshot_path": filepath,
            "url": url,
            "final_url": final_url,
            "platform": platform,
        }

    # 保留旧接口以兼容（或者让它们直接调用新接口，但建议外部直接调 process_any_url）
    async def get_xhs_info(self, url: str) -> Optional[Dict[str, Any]]:
        return await self.process_any_url(url)

    async def get_weibo_info(self, url: str) -> Optional[Dict[str, Any]]:
        return await self.process_any_url(url)

    async def get_dy_info(self, url: str) -> Optional[Dict[str, Any]]:
        return await self.process_any_url(url)

    # 移除旧的 _process_url 方法，逻辑已合并到 process_any_url

    async def _extract_ip(self, page: Page, platform: str, current_url: str) -> Optional[str]:
        """
        根据不同平台提取IP地址，支持多级回退策略
        """
        ip_address = None
        
        try:
            await self._dismiss_login_overlay(page, platform)
            if platform == "xhs":
                # 策略1: 作品页直接获取
                # 尝试多个可能的选择器位置
                # 1. 用户提供的路径 (修正后)
                # 2. 常见的底部日期位置类名
                selectors = [
                    '//*[@id="noteContainer"]/div[4]/div[2]/div[1]/div[3]/span[1]', # 用户指定的修正路径
                    '//*[@id="noteContainer"]/div[4]/div[2]/div[1]/div[2]/span[1]', # 旧路径
                    '.date', # 通用类名
                    '.note-scroller .date',
                    '.note-content .date'
                ]
                
                for selector in selectors:
                    try:
                        if await page.locator(selector).first.is_visible(timeout=1000):
                            text = await page.locator(selector).first.inner_text()
                            # 格式 "3天前 吉林" -> 取空格后
                            # 或者 "01-01 广东"
                            parts = text.strip().split(" ")
                            if len(parts) > 1:
                                ip_address = parts[-1]
                                logger.info("[{}] 从作品页获取到IP ({}): {}", platform, selector, ip_address)
                                return ip_address
                    except Exception as exc:
                        if _is_target_closed_error(exc):
                            raise
                        continue
                        
                logger.info("[{}] 作品页未找到IP，尝试进入主页...", platform)

                # 策略2: 进入用户主页
                # 点击用户名: //*[@id="noteContainer"]/div[4]/div[1]/div/div[1]/a[2]/span
                xpath_user_link = '//*[@id="noteContainer"]/div[4]/div[1]/div/div[1]/a[2]/span'
                try:
                    # 点击并等待新页面或当前页面跳转
                    # 小红书点击用户名通常是在当前页跳转或新标签，这里假设是链接跳转
                    # 为了稳妥，使用 wait_for_load_state
                    await page.click(xpath_user_link)
                    await page.wait_for_load_state("domcontentloaded")
                    await page.wait_for_timeout(2000) # 等待渲染
                    await self._dismiss_login_overlay(page, platform)

                    # 主页IP: //*[@id="userPageContainer"]/div[1]/div/div[2]/div[1]/div[1]/div[2]/div[2]/span[2]
                    # 格式: " IP属地：吉林"
                    xpath_profile_ip = '//*[@id="userPageContainer"]/div[1]/div/div[2]/div[1]/div[1]/div[2]/div[2]/span[2]'
                    if await page.locator(xpath_profile_ip).is_visible(timeout=5000):
                        text = await page.locator(xpath_profile_ip).inner_text()
                        if "：" in text:
                            ip_address = text.split("：")[-1].strip()
                            logger.info("[{}] 从主页获取到IP: {}", platform, ip_address)
                            return ip_address
                        elif ":" in text:
                            ip_address = text.split(":")[-1].strip()
                            logger.info("[{}] 从主页获取到IP: {}", platform, ip_address)
                            return ip_address
                except Exception as e:
                    if _is_target_closed_error(e):
                        raise
                    logger.info("[{}] 进入主页获取IP失败: {}", platform, e)

            elif platform == "weibo":
                # 策略1: 作品页直接获取
                # //*[@id="app"]/div[1]/div[2]/div[2]/main/div[1]/div/div[2]/article/div[2]/header/div[1]/div/div[2]/div/div/div[1]
                # 格式: "发布于 内蒙古"
                xpath_note = '//*[@id="app"]/div[1]/div[2]/div[2]/main/div[1]/div/div[2]/article/div[2]/header/div[1]/div/div[2]/div/div/div[1]'
                try:
                    if await page.locator(xpath_note).is_visible(timeout=2000):
                        text = await page.locator(xpath_note).inner_text()
                        text = text.replace("发布于", "").strip()
                        if text:
                            ip_address = text
                            logger.info("[{}] 从作品页获取到IP: {}", platform, ip_address)
                            return ip_address
                except Exception as exc:
                    if _is_target_closed_error(exc):
                        raise
                    logger.info("[{}] 作品页未找到IP，尝试进入主页...", platform)

                # 策略2: 构造主页链接跳转
                # 当前链接: https://weibo.com/5669907032/PAla4hPI9
                # 目标: https://weibo.com/5669907032
                try:
                    match = re.search(r'(https?://weibo\.com/\d+)', current_url)
                    if match:
                        home_url = match.group(1)
                        logger.info("[{}] 跳转到主页: {}", platform, home_url)
                        await page.goto(home_url, wait_until="domcontentloaded")
                        await page.wait_for_timeout(2000)

                        # 主页IP: //*[@id="app"]/div[1]/div[2]/div[2]/main/div[1]/div/div[2]/div[3]/div/div/div[1]/div[3]/div/div/div[2]/div/div[1]
                        # 格式: "IP属地：山西"
                        xpath_profile_ip = '//*[@id="app"]/div[1]/div[2]/div[2]/main/div[1]/div/div[2]/div[3]/div/div/div[1]/div[3]/div/div/div[2]/div/div[1]'
                        if await page.locator(xpath_profile_ip).is_visible(timeout=5000):
                            text = await page.locator(xpath_profile_ip).inner_text()
                            if "：" in text:
                                ip_address = text.split("：")[-1].strip()
                                logger.info("[{}] 从主页获取到IP: {}", platform, ip_address)
                                return ip_address
                    else:
                        logger.warning("[{}] 无法解析主页链接: {}", platform, current_url)
                except Exception as e:
                    if _is_target_closed_error(e):
                        raise
                    logger.info("[{}] 主页获取IP失败: {}", platform, e)

            elif platform == "douyin":
                # 策略: 必须去用户主页
                # 用户提供的两种 Full XPath  
                xpath_user_link_v1 = 'xpath=/html/body/div[2]/div[1]/div[4]/div[2]/div/div/main/div[2]/div[1]/div[1]/div/a/span/img'
                xpath_user_link_v2 = 'xpath=/html/body/div[2]/div[1]/div[4]/div[2]/div/div/div/div/div[2]/div/div[1]/div[1]/div/a/span/img'
                
                try:
                    # 抖音点击可能会打开新页面，需要处理
                    # 先尝试点击
                    target_locator = None
                    
                    # 1. 尝试第一种 XPath
                    loc1 = page.locator(xpath_user_link_v1).first
                    if await loc1.count() > 0:
                        # 检查可见性 (超时时间设为 1000ms，快速失败)
                        try:
                            if await loc1.is_visible(timeout=1000):
                                target_locator = loc1
                                logger.info("[{}] 使用 XPath V1 定位头像 (Visible)", platform)
                            else:
                                logger.warning("[{}] XPath V1 元素存在但不可见，尝试备用方案...", platform)
                        except Exception as exc:
                            if _is_target_closed_error(exc):
                                raise
                            logger.warning("[{}] XPath V1 可见性检查超时", platform)
                    
                    # 2. 如果首选失败，尝试第二种 XPath
                    if not target_locator:
                        loc2 = page.locator(xpath_user_link_v2).first
                        if await loc2.count() > 0:
                             try:
                                 if await loc2.is_visible(timeout=1000):
                                    target_locator = loc2
                                    logger.info("[{}] 使用 XPath V2 定位头像 (Visible)", platform)
                                 else:
                                     logger.warning("[{}] XPath V2 元素存在但不可见", platform)
                             except Exception as exc:
                                  if _is_target_closed_error(exc):
                                      raise
                                  logger.warning("[{}] XPath V2 可见性检查超时", platform)
                        else:
                             # 如果两个都没找到，尝试在移动端布局下的可能位置（可选）
                             logger.warning("[{}] 抖音用户头像元素未找到 (XPath V1/V2)", platform)
                             pass
                    
                    if target_locator:
                        new_page = None
                        try:
                            async with page.expect_popup() as popup_info:
                                await target_locator.click()
                            
                            new_page = await popup_info.value
                            await new_page.wait_for_load_state("domcontentloaded")
                            await new_page.wait_for_timeout(2000)
                            await self._dismiss_login_overlay(new_page, platform)
                            
                            # 在新页面查找IP
                            # 主页IP: //*[@id="user_detail_element"]/div/div[2]/div[2]/p/span[2]
                            # 格式: "IP属地：广东"
                            xpath_profile_ip = '//*[@id="user_detail_element"]/div/div[2]/div/div[1]/div[2]/p/span[2]'
                            if await new_page.locator(xpath_profile_ip).is_visible(timeout=5000):
                                text = await new_page.locator(xpath_profile_ip).inner_text()
                                if "：" in text:
                                    ip_address = text.split("：")[-1].strip()
                                    logger.info("[{}] 从主页获取到IP: {}", platform, ip_address)
                                    return ip_address
                                elif ":" in text:
                                    ip_address = text.split(":")[-1].strip()
                                    return ip_address
                        finally:
                            if new_page:
                                try:
                                    await new_page.close()
                                    logger.debug("[{}] 抖音个人主页已关闭", platform)
                                except Exception as e:
                                    if _is_target_closed_error(e):
                                        raise
                                    logger.warning("[{}] 关闭抖音个人主页失败: {}", platform, e)
                    else:
                        logger.warning("[{}] 未找到用户头像入口", platform)
                except Exception as e:
                     if _is_target_closed_error(e):
                         raise
                     # 如果没有弹出新页面，可能是SPA跳转，尝试在当前页面找
                     logger.info("[{}] 尝试Popup跳转失败，检查当前页面: {}", platform, e)
                     # 备用方案：如果是在当前页跳转
                     try:
                         # 这里的xpath可能在不同页面结构下不同，暂时只处理新页面情况
                         pass 
                     except Exception:
                         pass

        except Exception as e:
            if _is_target_closed_error(e):
                raise
            logger.error("[{}] IP提取过程发生未知错误: {}", platform, e)

        if not ip_address:
            logger.warning("[{}] 未能获取到IP属地。URL: {}", platform, current_url)
            
        return ip_address

if __name__ == "__main__":
    async def main():
        # 配置: 是否使用无头模式
        HEADLESS_MODE = False
        
        # 启动浏览器服务 (会自动检查登录，未登录会弹出窗口)
        await PlaywrightManager.start(headless=HEADLESS_MODE)
        
        checker = PlaywrightIpChecker()
        
        # 测试 URL (请填入真实链接)
        test_cases = [
            # ("xhs", "https://www.xiaohongshu.com/discovery/item/697d9456000000000e03fa2e?source=webshare&xhsshare=pc_web&xsec_token=ABF66cmefbqZ08OaTmolIxfyuLZ25qZ5rbTEyEhefTx-o=&xsec_source=pc_share"), 
            # ("weibo", "https://weibo.com/5669907032/PAla4hPI9"),
            ("douyin", "https://www.iesdouyin.com/share/video/7665215768238012020#ocr"),
        ]
        
        # 只是为了演示，这里使用 gather 并发执行
        tasks = []
        for platform, url in test_cases:
            if platform == "xhs":
                tasks.append(checker.get_xhs_info(url))
            elif platform == "weibo":
                tasks.append(checker.get_weibo_info(url))
            elif platform == "douyin":
                tasks.append(checker.get_dy_info(url))
        
        logger.info("开始并发执行测试任务...")
        results = await asyncio.gather(*tasks)
        
        for res in results:
            print(f"Result: {res}")
            
        # 停止服务
        await PlaywrightManager.stop()

    asyncio.run(main())
