# --- 所有正则表达式 (完整复制) ---
import asyncio
import os
from typing import Optional, Tuple

from loguru import logger

from src.config import global_config
from src.ding_talk.dd_hook import send_to_dd
from src.utils.commons import timeit, extract_author
from src.utils.deduplication import is_url_processed, is_username_processed

# --------------------------------------------------------------------------------------
# 步骤1：将关键词和IP检查分别封装成独立的异步函数
# --------------------------------------------------------------------------------------

async def run_keyword_check(msg) -> Optional[Tuple[str, Optional[str]]]:
    """
    执行关键词检查。如果匹配，返回 (处理好的消息文本, 截图路径)；否则返回 None。
    """
    if check_keyword(msg):
        logger.info("✅ 规则匹配：关键词")
        # 引用消息和普通消息清理对象不同
        content_to_clean = msg.quote_content if msg.type == 'quote' else msg.content
        cleaned_msg = msg_cleaner(content_to_clean)

        if msg.type == 'quote':
            final_msg = msg_restructure(quote_content=cleaned_msg, msg_content=msg.content)
            logger.debug("引用消息处理完成，最终消息: {}", final_msg)
            # 引用消息：尝试从被引用的内容中提取 URL
            source_content = msg.quote_content
        else:
            final_msg = msg_restructure(msg_content=cleaned_msg)
            # 文本消息：尝试从内容中提取 URL
            source_content = msg.content
            
        # 截图逻辑
        screenshot_path = None
        urls = global_config.URL_PATTERN.findall(source_content)
        if urls:
            url = urls[0]
            logger.info("关键词匹配成功，准备截图 URL: {}", url)
            from src.utils.playwright_utils import PlaywrightIpChecker
            checker = PlaywrightIpChecker()
            # 强制截图模式，使用临时文件
            data = await checker.process_any_url(url, force_screenshot_only=True, use_temp_file=True)
            if data and data.get("screenshot_path"):
                screenshot_path = data["screenshot_path"]
        
        return final_msg, screenshot_path
        
    return None

async def run_ip_check(msg) -> Optional[Tuple[str, Optional[str]]]:
    """
    执行IP检查。如果匹配，返回 (处理好的消息文本, 截图路径)；否则返回 None。
    """
    result_data = await ip_should_sent(msg.content)
    if result_data:
        logger.info("✅ 规则匹配：IP属地")
        cleaned_msg_text = msg_cleaner(msg.content)
        final_msg = msg_restructure(msg_content=cleaned_msg_text)
        
        # 从 result_data 中获取截图路径
        screenshot_path = result_data.get('screenshot_path')
        return final_msg, screenshot_path
    return None

# --------------------------------------------------------------------------------------
# 步骤2：重写主处理函数，实现并行竞赛逻辑
# --------------------------------------------------------------------------------------

@timeit(log_level="INFO")
async def async_process_message(msg, chat, dd_sender):
    """
    异步处理核心逻辑 (并行优化版)
    """
    logger.info("========== 监听到 [{}] 消息, 内容: {}... ==========", msg.type, msg.content.replace(chr(10), ' ')[:100])

    # 消息预处理
    if msg.type == 'quote':
        if msg.quote_content == None: 
            logger.warning("引用消息为 None: {}", msg.content)
            return
        msg.quote_content = msg.quote_content.replace("https:// ", "https://").replace("http:// ", "https://").replace("http://", "https://")
        msg.quote_content = msg_cleaner(msg.quote_content)
        # 如果是引用消息则直接判断关键词返回即可
        result = await run_keyword_check(msg)
        logger.debug("quote checked = {}", result)
        
        if result:
            text, img_path = result
            if dd_sender is None:
                await send_to_dd(msg=text)
            else:
                await dd_sender.send_mixed(text, img_path)
            
            # 清理临时文件
            if img_path and os.path.exists(img_path):
                try:
                    os.remove(img_path)
                except:
                    pass
        return
    elif msg.type == 'text':
        if msg.content == None: 
            logger.warning("消息为 None: {}", msg.content)
            return
        msg.content = msg.content.replace("https:// ", "https://").replace("http:// ", "https://").replace("http://", "https://")
        username = extract_author(msg.content)
        msg.content = msg_cleaner(msg.content)
    else:
        logger.warning("未处理消息类型, type: {}", msg.type)
        return

    # 提前提取URL，用于去重
    url_match = global_config.URL_PATTERN.search(msg.content)
    url_string = url_match.group(0) if url_match else None

    url_has_processed = await is_url_processed(url_string)
    username_has_processed = await is_username_processed(username)
    if global_config.SYSTEM_ENV == 'prod' and msg.type == 'text':
        if url_string and url_has_processed:
            logger.info("该 URL 已被处理，跳过: {}", url_string)
            return
        elif username and username_has_processed:
            logger.info("该 用户名 已被处理，跳过: {}", username)
            return

    # 优化后的串行逻辑（避免并发导致重复截图）
    
    # 1. 优先进行关键词检查
    # 关键词检查比较轻量（除非命中截图），且优先级较高
    # 如果关键词命中，直接返回，不再进行 IP 检查
    
    # 将 run_keyword_check 拆分为两步：check 和 screenshot
    # 但为了少改动，我们利用 run_keyword_check 内部逻辑：
    # 它如果没命中关键词，返回 None，很快
    # 如果命中了，它会去截图，这会比较慢
    
    # 现在的矛盾点是：run_keyword_check 内部做了截图，run_ip_check 内部也做了截图
    # 如果我们想并行，就必须接受可能得重复请求（浪费资源）
    # 如果我们想避免重复，就得串行，或者共享截图结果
    
    # 鉴于 Playwright 资源昂贵，建议改为串行：
    # 先跑关键词检查 -> 命中 -> 结束
    # 没命中 -> 跑 IP 检查 -> 命中 -> 结束
    
    # 但是，run_keyword_check 现在的实现是：命中关键词 -> 截图 -> 返回
    # 如果我们想“预先”检查关键词而不截图，需要拆分函数
    
    # 方案 A：简单串行
    keyword_result = await run_keyword_check(msg)
    if keyword_result:
        logger.info("✅ 关键词检查命中，处理完成。")
        text, img_path = keyword_result
        if dd_sender is None:
            await send_to_dd(msg=text)
        else:
            await dd_sender.send_mixed(text, img_path)
        
        # 清理
        if img_path and os.path.exists(img_path):
            try:
                os.remove(img_path)
            except:
                pass
        return

    # 2. 如果关键词没命中，且有 URL，再进行 IP 检查
    if url_string:
        ip_result = await run_ip_check(msg)
        if ip_result:
            logger.info("✅ IP检查命中，处理完成。")
            text, img_path = ip_result
            if dd_sender is None:
                await send_to_dd(msg=text)
            else:
                await dd_sender.send_mixed(text, img_path)
            
            # 清理
            if img_path and os.path.exists(img_path):
                try:
                    os.remove(img_path)
                except:
                    pass
            return
            
    logger.info("所有检查已完成，未匹配任何规则。")
    return

    # 下面是旧的并行代码，已注释废弃
    """
    # --- 并行竞赛开始 ---
    tasks = []
    # ...
    """

def check_keyword(msg) -> bool:
    is_quote_msg = msg.type == 'quote'

    # ----------- 情况一：处理引用消息 -----------
    if is_quote_msg:
        original_content = msg.quote_content
        cite_msg = msg.content

        logger.debug(
            "处理引用消息: [引用内容] " + original_content.replace('\n', ' ')[:100] + "... -> [回复] {}", cite_msg)
        # 引用消息不用管原消息内容, 只需要检查引用消息内容是否有关键字
        found_keyword = None
        # 优先检查车次
        train_match = global_config.SHENGYANG_TRAIN.search(cite_msg)
        if train_match:
            found_keyword = train_match.group(0)
            logger.debug("引用回复内容匹配到 [车次] 关键词: '{}'", found_keyword)
        else:
            # 如果没有车次，再检查站点
            station_match = global_config.SHENYANG_STATION.search(cite_msg)
            if station_match:
                found_keyword = station_match.group(0)
                logger.debug("引用回复内容匹配到 [站点] 关键词: '{}'", found_keyword)

        # 如果两个都没匹配到，则返回
        if not found_keyword:
            logger.info("引用回复内容关键词校验未通过")
            return False
        return True

    # ----------- 情况二：处理普通文本消息 -----------
    else:
        message = msg.content
        logger.debug("处理普通文本消息: {}", message.replace("\n", " "))

        # 核心逻辑1: URL 校验, 判断消息内是否有 URL 链接
        url_result = global_config.URL_PATTERN.search(message)
        if not url_result or global_config.SHENYANG_FILTER.search(message):
            logger.debug('普通文本消息 url_result 校验未通过')
            return False
        logger.debug("普通文本消息 url_result 校验通过: {}", url_result.group(0))

        # 核心逻辑2: 检查关键词

        # ======================= [分开检查并记录关键词] =======================
        found_keyword = None
        # 优先检查车次
        train_match = global_config.SHENGYANG_TRAIN.search(message)
        if train_match:
            found_keyword = train_match.group(0)
            logger.debug("匹配到 [车次] 关键词: '{}'", found_keyword)
        else:
            # 如果没有车次，再检查站点
            station_match = global_config.SHENYANG_STATION.search(message)
            if station_match:
                found_keyword = station_match.group(0)
                logger.debug("匹配到 [站点] 关键词: '{}'", found_keyword)

        # 如果两个都没匹配到，则返回
        if not found_keyword:
            logger.info('普通文本消息 SHENGYANG_TRAIN 和 SHENYANG_STATION 校验未通过')
            return False
        return True

def msg_cleaner(msg_content: str) -> str:
    """
    消息清理

    :param msg_content: 需要清理的消息内容
    """
    # 清理msg中需要用SHENYANG_DELETE清理掉的内容
    cleaned_message = global_config.SHENYANG_DELETE.sub('', msg_content)
    # 第二步：按行切分，去除空行（包括只含空格或被清空后的行）
    cleaned_lines = []
    for line in cleaned_message.splitlines():
        stripped = line.strip()
        if stripped:  # 只保留非空行
            cleaned_lines.append(stripped)
    formatted_msg = "\n".join(cleaned_lines)
    if cleaned_message != msg_content:
        logger.debug("☑️ 检测到 SHENYANG_DELETE 内容替换，原始文本已被删减为:\n{}", formatted_msg)
    else:
        logger.debug("✅ 未检测到需要替换的敏感词内容。")
    # 第四步：重组消息
    return formatted_msg

def msg_restructure(msg_content: str, quote_content: Optional[str] = None) -> str:
    """
    消息重构

    :param msg_content: 发出的内容，或者回复的内容（引用） A 引用了 B 的消息, msg_content = A
    :param quote_content: 被引用的消息内容. A 引用了 B 的消息, quote_content = B
    """
    if quote_content:
        restructured_msg = '沈阳处\n落查：' + msg_content + '\n' + quote_content
    else:
        restructured_msg = '沈阳处\n' + msg_content
    return restructured_msg

async def ip_should_sent(msg_content) -> Optional[dict]:
    """
    IP检测逻辑
    返回：如果匹配成功，返回包含信息的字典；如果不匹配，返回 None
    """
    # 关键词检查保留，作为快速过滤器
    keywords = ["xhs", "douyin", "weibo", "xiaohongshu", "kuaishou", "toutiao"]
    if any(keyword in msg_content for keyword in keywords):
        logger.debug("触发 [IP检测] 逻辑, msg: {}", msg_content.replace("\n", " ")[:100])
        # 尝试提取 URL
        urls = global_config.IP_PATTERN.findall(msg_content)
        if not urls:
            return None
        
        url = urls[0] # 取第一个URL
        logger.debug("提取到URL: {}", url)
        
        # 使用新的 Playwright 工具获取信息
        from src.utils.playwright_utils import PlaywrightIpChecker
        checker = PlaywrightIpChecker()

        # 直接调用统一入口，不区分平台
        data = await checker.process_any_url(url)

        if data is None:
            return None
        
        # 判断逻辑：
        # 1. 如果提取到了 IP (requires_ip 的平台)，必须匹配地址列表
        if data.get('true_address'):
            if data['true_address'] in global_config.ADDRESS_LIST:
                logger.info("IP地址匹配成功: {}", data['true_address'])
                return data # 返回完整数据对象，包含截图路径
            else:
                logger.debug("IP地址不匹配: {}", data['true_address'])
                return None
        
        # 2. 如果没提取到 IP，但属于仅截图平台 (platform != unknown)，则视为通过
        elif data.get('platform') != 'unknown':
             from src.config.global_config import PLATFORM_CONFIG
             # 检查是否是那些“不需要IP”的平台
             is_screenshot_only = False
             for domain in PLATFORM_CONFIG["screenshot_only"]:
                 if domain.split(".")[0] in data.get('platform', ''):
                     is_screenshot_only = True
                     break
             
             if is_screenshot_only:
                 logger.info("无需IP检测的平台 [{}]，直接通过", data['platform'])
                 return data # 返回完整数据对象
             else:
                 # 是需要IP的平台，但没取到IP -> 失败
                 return None

        return None
