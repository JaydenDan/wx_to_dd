import asyncio
import os
import sys
import time
import re
from typing import Optional, Dict, Any

# 将项目根目录添加到 sys.path，解决模块导入问题
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.config.global_config import PLATFORM_CONFIG, SCREENSHOT_DELAY
from playwright.async_api import async_playwright, Page, Browser, Playwright, BrowserContext
from loguru import logger

# 全局变量存储 Playwright 和 Browser 实例
_GLOBAL_PLAYWRIGHT: Optional[Playwright] = None
_GLOBAL_BROWSER: Optional[Browser] = None
# 新增：全局 BrowserContext 用于持久化存储 Cookies
_GLOBAL_CONTEXT: Optional[BrowserContext] = None
# 新增：全局信号量控制并发
_GLOBAL_SEMAPHORE: Optional[asyncio.Semaphore] = None

class PlaywrightManager:
    """
    全局 Playwright 管理器，实现浏览器复用，支持高并发。
    """
    # 用户数据目录，用于保存登录状态
    USER_DATA_DIR = os.path.join(os.getcwd(), "user_data")

    # 新增：记录当前是否为无头模式
    _CURRENT_HEADLESS: bool = True
    
    @staticmethod
    async def start(headless: bool = True, check_login: bool = True):
        """
        启动 Playwright Browser
        :param headless: 是否使用无头模式
        :param check_login: 是否在启动后检查登录状态
        """
        global _GLOBAL_PLAYWRIGHT, _GLOBAL_BROWSER, _GLOBAL_CONTEXT, _GLOBAL_SEMAPHORE
        
        # 记录当前启动模式
        PlaywrightManager._CURRENT_HEADLESS = headless

        if _GLOBAL_CONTEXT is not None:
            logger.info("Playwright Browser 已经启动，无需重复启动。")
            return

        # 初始化信号量，限制并发数为 15
        if _GLOBAL_SEMAPHORE is None:
            _GLOBAL_SEMAPHORE = asyncio.Semaphore(15)

        logger.info("正在启动 Playwright Browser (Headless: {})...", headless)
        try:
            _GLOBAL_PLAYWRIGHT = await async_playwright().start()
            
            # 使用 launch_persistent_context 来持久化存储登录状态
            if not os.path.exists(PlaywrightManager.USER_DATA_DIR):
                os.makedirs(PlaywrightManager.USER_DATA_DIR)
                
            _GLOBAL_CONTEXT = await _GLOBAL_PLAYWRIGHT.chromium.launch_persistent_context(
                user_data_dir=PlaywrightManager.USER_DATA_DIR,
                headless=headless,
                args=[
                    '--no-sandbox', 
                    '--disable-setuid-sandbox',
                    '--disable-blink-features=AutomationControlled', # 关键：禁用自动化特征
                ],
                ignore_default_args=["--enable-automation"], # 关键：忽略默认的自动化提示条
                viewport={'width': 1920, 'height': 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            
            # 注入脚本，进一步隐藏自动化特征
            await _GLOBAL_CONTEXT.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
            """)
            
            # 设置默认超时时间
            _GLOBAL_CONTEXT.set_default_timeout(30000)
            
            # 这里为了兼容旧代码的 Browser 概念，我们虽然有了 Context，但还是保留 Browser 变量引用的概念
            # launch_persistent_context 返回的是 Context，它没有 .new_context() 方法
            # 所以后续的并发逻辑需要稍微调整：直接使用 pages，或者如果需要隔离，可以使用 new_page()
            # 注意：Persistent Context 不能创建子 Context，所以所有页面共享 Cookie，正好符合登录态共享的需求
            
            logger.info("Playwright Browser (Persistent Context) 启动成功！")
            
            # 检查登录状态
            if check_login:
                await PlaywrightManager.check_login_status()

        except Exception as e:
            logger.error("Playwright Browser 启动失败: {}", e)

    @staticmethod
    async def check_login_status():
        """
        检查关键平台的登录状态，如果没有登录，则弹出窗口让用户登录
        """
        global _GLOBAL_CONTEXT
        if not _GLOBAL_CONTEXT:
            return

        platforms_to_check = {
            "douyin": "https://www.douyin.com/",
            "xhs": "https://www.xiaohongshu.com/",
            "kuaishou": "https://c.kuaishou.com/fw/photo/3xu8bwer63hrre2"
        }
        
        page = await _GLOBAL_CONTEXT.new_page()
        try:
            for name, url in platforms_to_check.items():
                logger.info("检查 [{}] 登录状态...", name)
                await page.goto(url, wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)
                
                is_logged_in = False
                
                # 简单的登录判断逻辑 (根据页面元素判断，需要根据实际情况调整)
                if name == "douyin":
                    # 抖音已登录通常会有头像或特定的个人中心入口
                    # 这是一个假设的选择器，实际需要确认
                    if await page.locator(".avatar-component").count() > 0 or await page.locator("div[data-e2e='user-info']").count() > 0: 
                        is_logged_in = True
                    # 也可以判断是否有登录按钮，如果有则未登录
                    elif await page.locator("button:has-text('登录')").count() > 0:
                        is_logged_in = False
                    else:
                        # 尝试通过 Cookie 判断
                        cookies = await _GLOBAL_CONTEXT.cookies(url)
                        for cookie in cookies:
                            if cookie['name'] == 'sessionid' or cookie['name'] == 'passport_csrf_token': # 示例 Cookie 名
                                is_logged_in = True
                                break
                                
                elif name == "xhs":
                    # 小红书判断逻辑
                    # 延长等待时间，确保页面元素加载
                    try:
                        # 优先等待遮罩或登录弹窗
                        await page.wait_for_selector(".reds-mask, .login-container, text='我'", timeout=5000)
                    except:
                        pass
                        
                    # 1. 优先检测遮罩层/登录弹窗 -> 绝对未登录
                    if (await page.locator(".reds-mask").count() > 0 or 
                        await page.locator(".login-container").count() > 0):
                        is_logged_in = False
                        logger.warning("[xhs] 检测到登录遮罩/弹窗，判定为未登录。")
                    
                    # 2. 反向检测: 如果有显著的登录按钮 -> 未登录
                    elif await page.locator("button:has-text('登录')").count() > 0:
                         is_logged_in = False
                         
                    # 3. 正向检测: 只有在没有遮罩且有特征元素时，才判定为已登录
                    elif (await page.locator("text='我'").count() > 0 or 
                          await page.locator(".reds-icon-bell").count() > 0): 
                        is_logged_in = True

                elif name == "kuaishou":
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
                # 复用上面的检查逻辑
                if platform_name == "douyin":
                    if await page.locator(".avatar-component").count() > 0 or await page.locator("div[data-e2e='user-info']").count() > 0:
                         is_logged_in = True
                    elif await page.locator("button:has-text('登录')").count() == 0:
                         # 简单的反向检查：没有登录按钮了，可能就登录了，或者页面变了
                         # 这里可以加更严格的 Cookie 检查
                         cookies = await _GLOBAL_CONTEXT.cookies(url)
                         for cookie in cookies:
                            if cookie['name'] == 'sessionid':
                                is_logged_in = True
                                break

                elif platform_name == "xhs":
                     # 延长等待时间
                     try:
                         await page.wait_for_selector(".reds-mask, .login-container, text='我'", timeout=2000)
                     except:
                         pass

                     if (await page.locator(".reds-mask").count() > 0 or 
                         await page.locator(".login-container").count() > 0):
                         is_logged_in = False
                     elif await page.locator("button:has-text('登录')").count() > 0:
                         is_logged_in = False
                     elif (await page.locator("text='我'").count() > 0 or 
                          await page.locator(".reds-icon-bell").count() > 0):
                        is_logged_in = True

                elif platform_name == "kuaishou":
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
        global _GLOBAL_PLAYWRIGHT, _GLOBAL_CONTEXT, _GLOBAL_SEMAPHORE
        if _GLOBAL_CONTEXT:
            await _GLOBAL_CONTEXT.close()
            _GLOBAL_CONTEXT = None
            logger.info("Playwright Context 已关闭。")
        
        if _GLOBAL_PLAYWRIGHT:
            await _GLOBAL_PLAYWRIGHT.stop()
            _GLOBAL_PLAYWRIGHT = None
            logger.info("Playwright 服务已停止。")
        
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

    async def process_any_url(self, url: str, force_screenshot_only: bool = False, use_temp_file: bool = False) -> Optional[Dict[str, Any]]:
        """
        统一入口：处理任意 URL，自动判断平台并分发
        :param url: 目标 URL
        :param force_screenshot_only: 强制仅截图模式（忽略 IP 获取）
        :param use_temp_file: 是否使用临时文件保存截图（用于发送后即弃的场景）
        """
        # 使用信号量控制并发
        async with PlaywrightManager.get_semaphore():
            return await self._process_any_url_internal(url, force_screenshot_only, use_temp_file)

    async def _process_any_url_internal(self, url: str, force_screenshot_only: bool = False, use_temp_file: bool = False, retry_count: int = 0) -> Optional[Dict[str, Any]]:
        """
        内部实现逻辑
        """
        context = PlaywrightManager.get_context()
        if context is None:
            logger.warning("Playwright Context 未初始化，尝试自动启动...")
            await PlaywrightManager.start(headless=True)
            context = PlaywrightManager.get_context()
            if context is None:
                logger.error("无法启动 Context，任务终止。")
                return None

        page = None
        try:
            try:
                page = await context.new_page()
            except Exception as e:
                # 捕获 TargetClosedError 或其他相关错误
                if "closed" in str(e).lower() or "target" in str(e).lower():
                    if retry_count < 1:
                        logger.warning("检测到浏览器已关闭，尝试重启并重试任务...")
                        await PlaywrightManager.stop()
                        # 重启时 check_login=False，避免重新进行登录校验
                        await PlaywrightManager.start(headless=True, check_login=False)
                        # 递归重试一次
                        return await self._process_any_url_internal(url, force_screenshot_only, use_temp_file, retry_count=1)
                    else:
                        logger.error("浏览器重启后依然失败，放弃任务。")
                        raise e
                else:
                    raise e

            logger.info("正在访问: {} (Force Screenshot: {})", url, force_screenshot_only)
            
            # 1. 访问 URL
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(1000)
            except Exception as e:
                logger.warning("页面加载可能不完整: {}", e)

            # 2. 获取最终跳转后的 URL 和域名
            final_url = page.url
            logger.info("最终 URL: {}", final_url)
            
            platform = "unknown"
            
            # 3. 域名匹配与策略分发
            requires_ip = False
            is_mobile = False
            
            # 检查是否需要移动端视口
            if "mobile_screen" in PLATFORM_CONFIG:
                for domain in PLATFORM_CONFIG["mobile_screen"]:
                    if domain in final_url:
                        is_mobile = True
                        break
            
            if is_mobile:
                logger.info("[{}] 切换至移动端视口 (430x932)", platform)
                await page.set_viewport_size({"width": 430, "height": 932})
            
            # 如果强制仅截图，则跳过 IP 检查配置
            if not force_screenshot_only:
                # 检查是否需要 IP 获取
                for domain in PLATFORM_CONFIG["ip_required"]:
                    if domain in final_url:
                        # 映射到内部平台标识
                        if "xiaohongshu" in domain or "xhslink" in domain:
                            platform = "xhs"
                        elif "douyin" in domain:
                            platform = "douyin"
                        elif "weibo" in domain:
                            platform = "weibo"
                        requires_ip = True
                        break
            else:
                logger.info("已启用强制截图模式，跳过 IP 获取逻辑。")
            
            # 检查是否仅截图 (用于识别平台名称)
            if not requires_ip:
                for domain in PLATFORM_CONFIG["screenshot_only"]:
                    if domain in final_url:
                        platform = domain.split(".")[0] # 如 kuaishou, toutiao, soulsmile
                        break
            
            # 如果是未知平台，默认只截图
            if platform == "unknown":
                logger.info("未知平台链接，将仅执行截图: {}", final_url)

            # 4. 特殊处理：检测遮罩 (针对小红书等)
            if platform == "xhs" and requires_ip:
                 if await page.locator(".reds-mask").count() > 0 or await page.locator(".login-container").count() > 0:
                     logger.warning("[{}] 检测到登录遮罩，可能登录态已失效！", platform)

            # 4.5 等待加载
            if SCREENSHOT_DELAY > 0:
                logger.info("[{}] 截图前额外等待 {} 秒...", platform, SCREENSHOT_DELAY)
                await asyncio.sleep(SCREENSHOT_DELAY)

            # 5. 截图
            if use_temp_file:
                import tempfile
                # 创建临时文件，不会自动删除，需要调用者处理，或者系统自动清理
                # 使用 delete=False 确保文件存在，关闭后可读取
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    filepath = tmp.name
                logger.info("[{}] 使用临时截图文件: {}", platform, filepath)
            else:
                filename = f"{platform}_{int(time.time())}.png"
                filepath = os.path.join(self.screenshot_dir, filename)

            try:
                await page.screenshot(path=filepath, full_page=False)
                if not use_temp_file:
                    logger.info("[{}] 截图已保存: {}", platform, filepath)
            except Exception as e:
                logger.error("[{}] 截图失败: {}", platform, e)
                # 如果截图失败且是临时文件，尝试清理
                if use_temp_file and os.path.exists(filepath):
                    try:
                        os.remove(filepath)
                    except:
                        pass
                filepath = None # 标记失败

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
                "platform": platform
            }

        except Exception as e:
            logger.exception("任务处理异常: {}", e)
            return None
        finally:
            if page:
                await page.close()

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
                    except:
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
                except Exception:
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
                    logger.info("[{}] 主页获取IP失败: {}", platform, e)

            elif platform == "douyin":
                # 策略: 必须去用户主页
                # 入口: //*[@id="douyin-right-container"]/div[2]/div/div/div[2]/div/div[1]/div[2]/a
                xpath_user_link = '//*[@id="douyin-right-container"]/div[2]/div/div/div[2]/div/div[1]/div[2]/a'
                try:
                    # 抖音点击可能会打开新页面，需要处理
                    # 先尝试点击
                    async with page.expect_popup() as popup_info:
                        await page.click(xpath_user_link)
                    
                    new_page = await popup_info.value
                    await new_page.wait_for_load_state("domcontentloaded")
                    await new_page.wait_for_timeout(2000)
                    
                    # 在新页面查找IP
                    # 主页IP: //*[@id="user_detail_element"]/div/div[2]/div[2]/p/span[2]
                    # 格式: "IP属地：广东"
                    xpath_profile_ip = '//*[@id="user_detail_element"]/div/div[2]/div[2]/p/span[2]'
                    if await new_page.locator(xpath_profile_ip).is_visible(timeout=5000):
                        text = await new_page.locator(xpath_profile_ip).inner_text()
                        if "：" in text:
                            ip_address = text.split("：")[-1].strip()
                            logger.info("[{}] 从主页获取到IP: {}", platform, ip_address)
                            # 这里如果不关闭新页面，context关闭时也会自动关闭
                            return ip_address
                        elif ":" in text:
                            ip_address = text.split(":")[-1].strip()
                            return ip_address
                except Exception as e:
                    # 如果没有弹出新页面，可能是SPA跳转，尝试在当前页面找
                     logger.info("[{}] 尝试Popup跳转失败，检查当前页面: {}", platform, e)
                     # 备用方案：如果是在当前页跳转
                     try:
                         # 这里的xpath可能在不同页面结构下不同，暂时只处理新页面情况
                         pass 
                     except Exception:
                         pass

        except Exception as e:
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
            ("xhs", "https://www.xiaohongshu.com/discovery/item/697d9456000000000e03fa2e?source=webshare&xhsshare=pc_web&xsec_token=ABF66cmefbqZ08OaTmolIxfyuLZ25qZ5rbTEyEhefTx-o=&xsec_source=pc_share"), 
            ("weibo", "https://weibo.com/5669907032/PAla4hPI9"),
            ("douyin", "https://www.douyin.com/video/7527531787717037370"),
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
