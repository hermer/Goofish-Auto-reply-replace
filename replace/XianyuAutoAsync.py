
import asyncio
import json
import re
import time
import base64
import os
from loguru import logger
import websockets
from utils.xianyu_utils import (
    decrypt, generate_mid, generate_uuid, trans_cookies,
    generate_device_id, generate_sign
)
from config import (
    WEBSOCKET_URL, HEARTBEAT_INTERVAL, HEARTBEAT_TIMEOUT,
    TOKEN_REFRESH_INTERVAL, TOKEN_RETRY_INTERVAL, COOKIES_STR,
    LOG_CONFIG, AUTO_REPLY, DEFAULT_HEADERS, WEBSOCKET_HEADERS,
    APP_CONFIG, API_ENDPOINTS
)
import sys
import aiohttp
from collections import defaultdict


class AutoReplyPauseManager:
    """自动回复暂停管理器"""
    def __init__(self):
        # 存储每个chat_id的暂停信息 {chat_id: pause_until_timestamp}
        self.paused_chats = {}

    def pause_chat(self, chat_id: str, cookie_id: str):
        """暂停指定chat_id的自动回复，使用账号特定的暂停时间"""
        # 获取账号特定的暂停时间
        try:
            from db_manager import db_manager
            pause_minutes = db_manager.get_cookie_pause_duration(cookie_id)
        except Exception as e:
            logger.error(f"获取账号 {cookie_id} 暂停时间失败: {e}，使用默认10分钟")
            pause_minutes = 10

        # 如果暂停时间为0，表示不暂停
        if pause_minutes == 0:
            logger.info(f"【{cookie_id}】检测到手动发出消息，但暂停时间设置为0，不暂停自动回复")
            return

        pause_duration_seconds = pause_minutes * 60
        pause_until = time.time() + pause_duration_seconds
        self.paused_chats[chat_id] = pause_until

        # 计算暂停结束时间
        end_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(pause_until))
        logger.info(f"【{cookie_id}】检测到手动发出消息，chat_id {chat_id} 自动回复暂停{pause_minutes}分钟，恢复时间: {end_time}")

    def is_chat_paused(self, chat_id: str) -> bool:
        """检查指定chat_id是否处于暂停状态"""
        if chat_id not in self.paused_chats:
            return False

        current_time = time.time()
        pause_until = self.paused_chats[chat_id]

        if current_time >= pause_until:
            # 暂停时间已过，移除记录
            del self.paused_chats[chat_id]
            return False

        return True

    def get_remaining_pause_time(self, chat_id: str) -> int:
        """获取指定chat_id剩余暂停时间（秒）"""
        if chat_id not in self.paused_chats:
            return 0

        current_time = time.time()
        pause_until = self.paused_chats[chat_id]
        remaining = max(0, int(pause_until - current_time))

        return remaining

    def cleanup_expired_pauses(self):
        """清理已过期的暂停记录"""
        current_time = time.time()
        expired_chats = [chat_id for chat_id, pause_until in self.paused_chats.items()
                        if current_time >= pause_until]

        for chat_id in expired_chats:
            del self.paused_chats[chat_id]


# 全局暂停管理器实例
pause_manager = AutoReplyPauseManager()

# 日志配置
log_dir = 'logs'
os.makedirs(log_dir, exist_ok=True)
log_path = os.path.join(log_dir, f"xianyu_{time.strftime('%Y-%m-%d')}.log")
logger.remove()
logger.add(
    log_path,
    rotation=LOG_CONFIG.get('rotation', '1 day'),
    retention=LOG_CONFIG.get('retention', '7 days'),
    compression=LOG_CONFIG.get('compression', 'zip'),
    level=LOG_CONFIG.get('level', 'INFO'),
    format=LOG_CONFIG.get('format', '<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>'),
    encoding='utf-8',
    enqueue=True
)
logger.add(
    sys.stdout,
    level=LOG_CONFIG.get('level', 'INFO'),
    format=LOG_CONFIG.get('format', '<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>'),
    enqueue=True
)

class XianyuLive:
    # 类级别的锁字典，为每个order_id维护一个锁（用于自动发货）
    _order_locks = defaultdict(lambda: asyncio.Lock())
    # 记录锁的最后使用时间，用于清理
    _lock_usage_times = {}
    # 记录锁的持有状态和释放时间 {lock_key: {'locked': bool, 'release_time': float, 'task': asyncio.Task}}
    _lock_hold_info = {}

    # 独立的锁字典，用于订单详情获取（不使用延迟锁机制）
    _order_detail_locks = defaultdict(lambda: asyncio.Lock())
    # 记录订单详情锁的使用时间
    _order_detail_lock_times = {}

    # 商品详情缓存（24小时有效）
    _item_detail_cache = {}  # {item_id: {'detail': str, 'timestamp': float}}
    _item_detail_cache_lock = asyncio.Lock()

    # 类级别的实例管理字典，用于API调用
    _instances = {}  # {cookie_id: XianyuLive实例}
    _instances_lock = asyncio.Lock()
    
    def _safe_str(self, e):
        """安全地将异常转换为字符串"""
        try:
            return str(e)
        except:
            try:
                return repr(e)
            except:
                return "未知错误"

    def __init__(self, cookies_str=None, cookie_id: str = "default", user_id: int = None):
        """初始化闲鱼直播类"""
        logger.info(f"【{cookie_id}】开始初始化XianyuLive...")

        if not cookies_str:
            cookies_str = COOKIES_STR
        if not cookies_str:
            raise ValueError("未提供cookies，请在global_config.yml中配置COOKIES_STR或通过参数传入")

        logger.info(f"【{cookie_id}】解析cookies...")
        self.cookies = trans_cookies(cookies_str)
        logger.info(f"【{cookie_id}】cookies解析完成，包含字段: {list(self.cookies.keys())}")

        self.cookie_id = cookie_id  # 唯一账号标识
        self.cookies_str = cookies_str  # 保存原始cookie字符串
        self.user_id = user_id  # 保存用户ID，用于token刷新时保持正确的所有者关系
        self.base_url = WEBSOCKET_URL

        if 'unb' not in self.cookies:
            raise ValueError(f"【{cookie_id}】Cookie中缺少必需的'unb'字段，当前字段: {list(self.cookies.keys())}")

        self.myid = self.cookies['unb']
        logger.info(f"【{cookie_id}】用户ID: {self.myid}")
        self.device_id = generate_device_id(self.myid)

        # 心跳相关配置
        self.heartbeat_interval = HEARTBEAT_INTERVAL
        self.heartbeat_timeout = HEARTBEAT_TIMEOUT
        self.last_heartbeat_time = 0
        self.last_heartbeat_response = 0
        self.heartbeat_task = None
        self.ws = None

        # Token刷新相关配置
        self.token_refresh_interval = TOKEN_REFRESH_INTERVAL
        self.token_retry_interval = TOKEN_RETRY_INTERVAL
        self.last_token_refresh_time = 0
        self.current_token = None
        self.token_refresh_task = None
        self.connection_restart_flag = False  # 连接重启标志
        self.token_failure_count = 0 # Token 刷新失败计数
 
        # 通知防重复机制
        self.last_notification_time = {}  # 记录每种通知类型的最后发送时间
        self.notification_cooldown = 300  # 5分钟内不重复发送相同类型的通知
        self.token_refresh_notification_cooldown = 18000  # Token刷新异常通知冷却时间：3小时

        # 自动发货防重复机制
        self.last_delivery_time = {}  # 记录每个商品的最后发货时间
        self.delivery_cooldown = 600  # 10分钟内不重复发货

        # 自动确认发货防重复机制
        self.confirmed_orders = {}  # 记录已确认发货的订单，防止重复确认
        self.order_confirm_cooldown = 600  # 10分钟内不重复确认同一订单

        # 自动发货已发送订单记录
        self.delivery_sent_orders = set()  # 记录已发货的订单ID，防止重复发货

        self.session = None  # 用于API调用的aiohttp session

        # 启动定期清理过期暂停记录的任务
        self.cleanup_task = None

        # Cookie刷新定时任务
        self.cookie_refresh_task = None
        self.cookie_refresh_interval = 1200  # 1小时 = 3600秒
        self.last_cookie_refresh_time = 0
        self.cookie_refresh_running = False  # 防止重复执行Cookie刷新
        self.cookie_refresh_enabled = True  # 是否启用Cookie刷新功能

        # 扫码登录Cookie刷新标志
        self.last_qr_cookie_refresh_time = 0  # 记录上次扫码登录Cookie刷新时间
        self.qr_cookie_refresh_cooldown = 600  # 扫码登录Cookie刷新后的冷却时间：10分钟

        # 消息接收标识 - 用于控制Cookie刷新
        self.last_message_received_time = 0  # 记录上次收到消息的时间
        self.message_cookie_refresh_cooldown = 300  # 收到消息后5分钟内不执行Cookie刷新

        # WebSocket连接监控
        self.connection_failures = 0  # 连续连接失败次数
        self.max_connection_failures = 5  # 最大连续失败次数
        self.last_successful_connection = 0  # 上次成功连接时间

        # 注册实例到类级别字典（用于API调用）
        self._register_instance()

    def _register_instance(self):
        """注册当前实例到类级别字典"""
        try:
            # 使用同步方式注册，避免在__init__中使用async
            XianyuLive._instances[self.cookie_id] = self
            logger.debug(f"【{self.cookie_id}】实例已注册到全局字典")
        except Exception as e:
            logger.error(f"【{self.cookie_id}】注册实例失败: {self._safe_str(e)}")

    def _unregister_instance(self):
        """从类级别字典中注销当前实例"""
        try:
            if self.cookie_id in XianyuLive._instances:
                del XianyuLive._instances[self.cookie_id]
                logger.debug(f"【{self.cookie_id}】实例已从全局字典中注销")
        except Exception as e:
            logger.error(f"【{self.cookie_id}】注销实例失败: {self._safe_str(e)}")

    @classmethod
    def get_instance(cls, cookie_id: str):
        """获取指定cookie_id的XianyuLive实例"""
        return cls._instances.get(cookie_id)

    @classmethod
    def get_all_instances(cls):
        """获取所有活跃的XianyuLive实例"""
        return dict(cls._instances)

    @classmethod
    def get_instance_count(cls):
        """获取当前活跃实例数量"""
        return len(cls._instances)

    def is_auto_confirm_enabled(self) -> bool:
        """检查当前账号是否启用自动确认发货"""
        try:
            from db_manager import db_manager
            return db_manager.get_auto_confirm(self.cookie_id)
        except Exception as e:
            logger.error(f"【{self.cookie_id}】获取自动确认发货设置失败: {self._safe_str(e)}")
            return True  # 出错时默认启用



    def can_auto_delivery(self, order_id: str) -> bool:
        """检查是否可以进行自动发货（防重复发货）- 基于订单ID"""
        if not order_id:
            # 如果没有订单ID，则不进行冷却检查，允许发货
            return True

        current_time = time.time()
        last_delivery = self.last_delivery_time.get(order_id, 0)

        if current_time - last_delivery < self.delivery_cooldown:
            logger.info(f"【{self.cookie_id}】订单 {order_id} 在冷却期内，跳过自动发货")
            return False

        return True

    def mark_delivery_sent(self, order_id: str):
        """标记订单已发货"""
        self.delivery_sent_orders.add(order_id)
        logger.info(f"【{self.cookie_id}】订单 {order_id} 已标记为发货")

    async def _delayed_lock_release(self, lock_key: str, delay_minutes: int = 10):
        """
        延迟释放锁的异步任务

        Args:
            lock_key: 锁的键
            delay_minutes: 延迟时间（分钟），默认10分钟
        """
        try:
            delay_seconds = delay_minutes * 60
            logger.info(f"【{self.cookie_id}】订单锁 {lock_key} 将在 {delay_minutes} 分钟后释放")

            # 等待指定时间
            await asyncio.sleep(delay_seconds)

            # 检查锁是否仍然存在且需要释放
            if lock_key in self._lock_hold_info:
                lock_info = self._lock_hold_info[lock_key]
                if lock_info.get('locked', False):
                    # 释放锁
                    lock_info['locked'] = False
                    lock_info['release_time'] = time.time()
                    logger.info(f"【{self.cookie_id}】订单锁 {lock_key} 延迟释放完成")

                    # 清理锁信息（可选，也可以保留用于统计）
                    # del self._lock_hold_info[lock_key]

        except asyncio.CancelledError:
            logger.info(f"【{self.cookie_id}】订单锁 {lock_key} 延迟释放任务被取消")
        except Exception as e:
            logger.error(f"【{self.cookie_id}】订单锁 {lock_key} 延迟释放失败: {self._safe_str(e)}")

    def is_lock_held(self, lock_key: str) -> bool:
        """
        检查指定的锁是否仍在持有状态

        Args:
            lock_key: 锁的键

        Returns:
            bool: True表示锁仍在持有，False表示锁已释放或不存在
        """
        if lock_key not in self._lock_hold_info:
            return False

        lock_info = self._lock_hold_info[lock_key]
        return lock_info.get('locked', False)

    def cleanup_expired_locks(self, max_age_hours: int = 24):
        """
        清理过期的锁（包括自动发货锁和订单详情锁）

        Args:
            max_age_hours: 锁的最大保留时间（小时），默认24小时
        """
        try:
            current_time = time.time()
            max_age_seconds = max_age_hours * 3600

            # 清理自动发货锁
            expired_delivery_locks = []
            for order_id, last_used in self._lock_usage_times.items():
                if current_time - last_used > max_age_seconds:
                    expired_delivery_locks.append(order_id)

            # 清理过期的自动发货锁
            for order_id in expired_delivery_locks:
                if order_id in self._order_locks:
                    del self._order_locks[order_id]
                if order_id in self._lock_usage_times:
                    del self._lock_usage_times[order_id]
                # 清理锁持有信息
                if order_id in self._lock_hold_info:
                    lock_info = self._lock_hold_info[order_id]
                    # 取消延迟释放任务
                    if 'task' in lock_info and lock_info['task']:
                        lock_info['task'].cancel()
                    del self._lock_hold_info[order_id]

            # 清理订单详情锁
            expired_detail_locks = []
            for order_id, last_used in self._order_detail_lock_times.items():
                if current_time - last_used > max_age_seconds:
                    expired_detail_locks.append(order_id)

            # 清理过期的订单详情锁
            for order_id in expired_detail_locks:
                if order_id in self._order_detail_locks:
                    del self._order_detail_locks[order_id]
                if order_id in self._order_detail_lock_times:
                    del self._order_detail_lock_times[order_id]

            total_expired = len(expired_delivery_locks) + len(expired_detail_locks)
            if total_expired > 0:
                logger.info(f"【{self.cookie_id}】清理了 {total_expired} 个过期锁 (发货锁: {len(expired_delivery_locks)}, 详情锁: {len(expired_detail_locks)})")
                logger.debug(f"【{self.cookie_id}】当前锁数量 - 发货锁: {len(self._order_locks)}, 详情锁: {len(self._order_detail_locks)}")

        except Exception as e:
            logger.error(f"【{self.cookie_id}】清理过期锁时发生错误: {self._safe_str(e)}")

    

    def _is_auto_delivery_trigger(self, message: str) -> bool:
        """检查消息是否为自动发货触发关键字"""
        # 定义所有自动发货触发关键字
        auto_delivery_keywords = [
            # 系统消息
            '[我已付款，等待你发货]',
            '[已付款，待发货]',
            '我已付款，等待你发货',
            '[记得及时发货]',
        ]

        # 检查消息是否包含任何触发关键字
        for keyword in auto_delivery_keywords:
            if keyword in message:
                return True

        return False

    def _extract_order_id(self, message: dict) -> str:
        """从消息中提取订单ID"""
        try:
            order_id = None

            # 先查看消息的完整结构
            logger.debug(f"【{self.cookie_id}】🔍 完整消息结构: {message}")

            # 检查message['1']的结构，处理可能是列表、字典或字符串的情况
            message_1 = message.get('1', {})
            content_json_str = ''

            if isinstance(message_1, dict):
                logger.debug(f"【{self.cookie_id}】🔍 message['1'] 是字典，keys: {list(message_1.keys())}")

                # 检查message['1']['6']的结构
                message_1_6 = message_1.get('6', {})
                if isinstance(message_1_6, dict):
                    logger.debug(f"【{self.cookie_id}】🔍 message['1']['6'] 是字典，keys: {list(message_1_6.keys())}")
                    # 方法1: 从button的targetUrl中提取orderId
                    content_json_str = message_1_6.get('3', {}).get('5', '') if isinstance(message_1_6.get('3', {}), dict) else ''
                else:
                    logger.debug(f"【{self.cookie_id}】🔍 message['1']['6'] 不是字典: {type(message_1_6)}")

            elif isinstance(message_1, list):
                logger.debug(f"【{self.cookie_id}】🔍 message['1'] 是列表，长度: {len(message_1)}")
                # 如果message['1']是列表，跳过这种提取方式

            elif isinstance(message_1, str):
                logger.debug(f"【{self.cookie_id}】🔍 message['1'] 是字符串，长度: {len(message_1)}")
                # 如果message['1']是字符串，跳过这种提取方式

            else:
                logger.debug(f"【{self.cookie_id}】🔍 message['1'] 未知类型: {type(message_1)}")
                # 其他类型，跳过这种提取方式

            if content_json_str:
                try:
                    content_data = json.loads(content_json_str)

                    # 方法1a: 从button的targetUrl中提取orderId
                    target_url = content_data.get('dxCard', {}).get('item', {}).get('main', {}).get('exContent', {}).get('button', {}).get('targetUrl', '')
                    if target_url:
                        # 从URL中提取orderId参数
                        order_match = re.search(r'orderId=(\d+)', target_url)
                        if order_match:
                            order_id = order_match.group(1)
                            logger.info(f'【{self.cookie_id}】✅ 从button提取到订单ID: {order_id}')

                    # 方法1b: 从main的targetUrl中提取order_detail的id
                    if not order_id:
                        main_target_url = content_data.get('dxCard', {}).get('item', {}).get('main', {}).get('targetUrl', '')
                        if main_target_url:
                            order_match = re.search(r'order_detail\?id=(\d+)', main_target_url)
                            if order_match:
                                order_id = order_match.group(1)
                                logger.info(f'【{self.cookie_id}】✅ 从main targetUrl提取到订单ID: {order_id}')

                except Exception as parse_e:
                    logger.debug(f"解析内容JSON失败: {parse_e}")

            # 方法2: 从dynamicOperation中的order_detail URL提取orderId
            if not order_id and content_json_str:
                try:
                    content_data = json.loads(content_json_str)
                    dynamic_target_url = content_data.get('dynamicOperation', {}).get('changeContent', {}).get('dxCard', {}).get('item', {}).get('main', {}).get('exContent', {}).get('button', {}).get('targetUrl', '')
                    if dynamic_target_url:
                        # 从order_detail URL中提取id参数
                        order_match = re.search(r'order_detail\?id=(\d+)', dynamic_target_url)
                        if order_match:
                            order_id = order_match.group(1)
                            logger.info(f'【{self.cookie_id}】✅ 从order_detail提取到订单ID: {order_id}')
                except Exception as parse_e:
                    logger.debug(f"解析dynamicOperation JSON失败: {parse_e}")

            # 方法3: 如果前面的方法都失败，尝试在整个消息中搜索订单ID模式
            if not order_id:
                try:
                    # 将整个消息转换为字符串进行搜索
                    message_str = str(message)

                    # 搜索各种可能的订单ID模式
                    patterns = [
                        r'orderId[=:](\d{10,})',  # orderId=123456789 或 orderId:123456789
                        r'order_detail\?id=(\d{10,})',  # order_detail?id=123456789
                        r'"id"\s*:\s*"?(\d{10,})"?',  # "id":"123456789" 或 "id":123456789
                        r'bizOrderId[=:](\d{10,})',  # bizOrderId=123456789
                    ]

                    for pattern in patterns:
                        matches = re.findall(pattern, message_str)
                        if matches:
                            # 取第一个匹配的订单ID
                            order_id = matches[0]
                            logger.info(f'【{self.cookie_id}】✅ 从消息字符串中提取到订单ID: {order_id} (模式: {pattern})')
                            break

                except Exception as search_e:
                    logger.debug(f"在消息字符串中搜索订单ID失败: {search_e}")

            if order_id:
                logger.info(f'【{self.cookie_id}】🎯 最终提取到订单ID: {order_id}')
            else:
                logger.debug(f'【{self.cookie_id}】❌ 未能从消息中提取到订单ID')

            return order_id

        except Exception as e:
            logger.error(f"【{self.cookie_id}】提取订单ID失败: {self._safe_str(e)}")
            return None

    async def _handle_auto_delivery(self, websocket, message: dict, send_user_name: str, send_user_id: str,
                                   item_id: str, chat_id: str, msg_time: str):
        """统一处理自动发货逻辑"""
        try:
            # 检查商品是否属于当前cookies
            if item_id and item_id != "未知商品":
                try:
                    from db_manager import db_manager
                    item_info = db_manager.get_item_info(self.cookie_id, item_id)
                    if not item_info:
                        logger.warning(f'[{msg_time}] 【{self.cookie_id}】❌ 商品 {item_id} 不属于当前账号，跳过自动发货')
                        return
                    logger.debug(f'[{msg_time}] 【{self.cookie_id}】✅ 商品 {item_id} 归属验证通过')
                except Exception as e:
                    logger.error(f'[{msg_time}] 【{self.cookie_id}】检查商品归属失败: {self._safe_str(e)}，跳过自动发货')
                    return

            # 提取订单ID
            order_id = self._extract_order_id(message)

            # 如果order_id不存在，直接返回
            if not order_id:
                logger.warning(f'[{msg_time}] 【{self.cookie_id}】❌ 未能提取到订单ID，跳过自动发货')
                return

            # 订单ID已提取，将在自动发货时进行确认发货处理
            logger.info(f'[{msg_time}] 【{self.cookie_id}】提取到订单ID: {order_id}，将在自动发货时处理确认发货')

            # 使用订单ID作为锁的键
            lock_key = order_id

            # 第一重检查：延迟锁状态（在获取锁之前检查，避免不必要的等待）
            if self.is_lock_held(lock_key):
                logger.info(f'[{msg_time}] 【{self.cookie_id}】🔒【提前检查】订单 {lock_key} 延迟锁仍在持有状态，跳过发货')
                return

            # 第二重检查：基于时间的冷却机制
            if not self.can_auto_delivery(order_id):
                logger.info(f'[{msg_time}] 【{self.cookie_id}】订单 {order_id} 在冷却期内，跳过发货')
                return

            # 获取或创建该订单的锁
            order_lock = self._order_locks[lock_key]

            # 更新锁的使用时间
            self._lock_usage_times[lock_key] = time.time()

            # 使用异步锁防止同一订单的并发处理
            async with order_lock:
                logger.info(f'[{msg_time}] 【{self.cookie_id}】获取订单锁成功: {lock_key}，开始处理自动发货')

                # 第三重检查：获取锁后再次检查延迟锁状态（双重检查，防止在等待锁期间状态发生变化）
                if self.is_lock_held(lock_key):
                    logger.info(f'[{msg_time}] 【{self.cookie_id}】订单 {lock_key} 在获取锁后检查发现延迟锁仍持有，跳过发货')
                    return

                # 第四重检查：获取锁后再次检查冷却状态
                if not self.can_auto_delivery(order_id):
                    logger.info(f'[{msg_time}] 【{self.cookie_id}】订单 {order_id} 在获取锁后检查发现仍在冷却期，跳过发货')
                    return

                # 构造用户URL
                user_url = f'https://www.goofish.com/personal?userId={send_user_id}'

                # 自动发货逻辑
                try:
                    # 设置默认标题（将通过API获取真实商品信息）
                    item_title = "待获取商品信息"

                    logger.info(f"【{self.cookie_id}】准备自动发货: item_id={item_id}, item_title={item_title}")

                    # 检查是否需要多数量发货
                    from db_manager import db_manager
                    quantity_to_send = 1  # 默认发送1个

                    # 检查商品是否开启了多数量发货
                    multi_quantity_delivery = db_manager.get_item_multi_quantity_delivery_status(self.cookie_id, item_id)

                    if multi_quantity_delivery and order_id:
                        logger.info(f"商品 {item_id} 开启了多数量发货，获取订单详情...")
                        try:
                            # 使用现有方法获取订单详情
                            order_detail = await self.fetch_order_detail_info(order_id, item_id, send_user_id)
                            if order_detail and order_detail.get('quantity'):
                                try:
                                    order_quantity = int(order_detail['quantity'])
                                    if order_quantity > 1:
                                        quantity_to_send = order_quantity
                                        logger.info(f"从订单详情获取数量: {order_quantity}，将发送 {quantity_to_send} 个卡券")
                                    else:
                                        logger.info(f"订单数量为 {order_quantity}，发送单个卡券")
                                except (ValueError, TypeError):
                                    logger.warning(f"订单数量格式无效: {order_detail.get('quantity')}，发送单个卡券")
                            else:
                                logger.info(f"未获取到订单数量信息，发送单个卡券")
                        except Exception as e:
                            logger.error(f"获取订单详情失败: {self._safe_str(e)}，发送单个卡券")
                    elif not multi_quantity_delivery:
                        logger.info(f"商品 {item_id} 未开启多数量发货，发送单个卡券")
                    else:
                        logger.info(f"无订单ID，发送单个卡券")

                    # 多次调用自动发货方法，每次获取不同的内容
                    delivery_contents = []
                    success_count = 0

                    for i in range(quantity_to_send):
                        try:
                            # 每次调用都可能获取不同的内容（API卡券、批量数据等）
                            delivery_content = await self._auto_delivery(item_id, item_title, order_id, send_user_id)
                            if delivery_content:
                                delivery_contents.append(delivery_content)
                                success_count += 1
                                if quantity_to_send > 1:
                                    logger.info(f"第 {i+1}/{quantity_to_send} 个卡券内容获取成功")
                            else:
                                logger.warning(f"第 {i+1}/{quantity_to_send} 个卡券内容获取失败")
                        except Exception as e:
                            logger.error(f"第 {i+1}/{quantity_to_send} 个卡券获取异常: {self._safe_str(e)}")

                    if delivery_contents:
                        # 标记已发货（防重复）- 基于订单ID
                        self.mark_delivery_sent(order_id)

                        # 标记锁为持有状态，并启动延迟释放任务
                        self._lock_hold_info[lock_key] = {
                            'locked': True,
                            'lock_time': time.time(),
                            'release_time': None,
                            'task': None
                        }

                        # 启动延迟释放锁的异步任务（10分钟后释放）
                        delay_task = asyncio.create_task(self._delayed_lock_release(lock_key, delay_minutes=10))
                        self._lock_hold_info[lock_key]['task'] = delay_task

                        # 发送所有获取到的发货内容
                        for i, delivery_content in enumerate(delivery_contents):
                            try:
                                # 检查是否是图片发送标记
                                if delivery_content.startswith("__IMAGE_SEND__"):
                                    # 提取卡券ID和图片URL
                                    image_data = delivery_content.replace("__IMAGE_SEND__", "")
                                    if "|" in image_data:
                                        card_id_str, image_url = image_data.split("|", 1)
                                        try:
                                            card_id = int(card_id_str)
                                        except ValueError:
                                            logger.error(f"无效的卡券ID: {card_id_str}")
                                            card_id = None
                                    else:
                                        # 兼容旧格式（没有卡券ID）
                                        card_id = None
                                        image_url = image_data

                                    # 发送图片消息
                                    await self.send_image_msg(websocket, chat_id, send_user_id, image_url, card_id=card_id)
                                    if len(delivery_contents) > 1:
                                        logger.info(f'[{msg_time}] 【多数量自动发货图片】第 {i+1}/{len(delivery_contents)} 张已向 {user_url} 发送图片: {image_url}')
                                    else:
                                        logger.info(f'[{msg_time}] 【自动发货图片】已向 {user_url} 发送图片: {image_url}')

                                    # 多数量发货时，消息间隔1秒
                                    if len(delivery_contents) > 1 and i < len(delivery_contents) - 1:
                                        await asyncio.sleep(1)

                                else:
                                    # 普通文本发货内容
                                    await self.send_msg(websocket, chat_id, send_user_id, delivery_content)
                                    if len(delivery_contents) > 1:
                                        logger.info(f'[{msg_time}] 【多数量自动发货】第 {i+1}/{len(delivery_contents)} 条已向 {user_url} 发送发货内容')
                                    else:
                                        logger.info(f'[{msg_time}] 【自动发货】已向 {user_url} 发送发货内容')

                                    # 多数量发货时，消息间隔1秒
                                    if len(delivery_contents) > 1 and i < len(delivery_contents) - 1:
                                        await asyncio.sleep(1)

                            except Exception as e:
                                logger.error(f"发送第 {i+1} 条消息失败: {self._safe_str(e)}")

                        # 发送成功通知
                        if len(delivery_contents) > 1:
                            await self.send_delivery_failure_notification(send_user_name, send_user_id, item_id, f"多数量发货成功，共发送 {len(delivery_contents)} 个卡券", chat_id)
                        else:
                            await self.send_delivery_failure_notification(send_user_name, send_user_id, item_id, "发货成功", chat_id)
                    else:
                        logger.warning(f'[{msg_time}] 【自动发货】未找到匹配的发货规则或获取发货内容失败')
                        # 发送自动发货失败通知
                        await self.send_delivery_failure_notification(send_user_name, send_user_id, item_id, "未找到匹配的发货规则或获取发货内容失败", chat_id)

                except Exception as e:
                    logger.error(f"自动发货处理异常: {self._safe_str(e)}")
                    # 发送自动发货异常通知
                    await self.send_delivery_failure_notification(send_user_name, send_user_id, item_id, f"自动发货处理异常: {str(e)}", chat_id)

                logger.info(f'[{msg_time}] 【{self.cookie_id}】订单锁释放: {lock_key}，自动发货处理完成')

        except Exception as e:
            logger.error(f"统一自动发货处理异常: {self._safe_str(e)}")



    async def refresh_token(self):
        """刷新token"""
        try:
            logger.info(f"【{self.cookie_id}】开始刷新token...")
            # 生成更精确的时间戳
            timestamp = str(int(time.time() * 1000))

            params = {
                'jsv': '2.7.2',
                'appKey': '34839810',
                't': timestamp,
                'sign': '',
                'v': '1.0',
                'type': 'originaljson',
                'accountSite': 'xianyu',
                'dataType': 'json',
                'timeout': '20000',
                'api': 'mtop.taobao.idlemessage.pc.login.token',
                'sessionOption': 'AutoLoginOnly',
                'dangerouslySetWindvaneParams': '%5Bobject%20Object%5D',
                'smToken': 'token',
                'queryToken': 'sm',
                'sm': 'sm',
                'spm_cnt': 'a21ybx.im.0.0',
                'spm_pre': 'a21ybx.home.sidebar.1.4c053da6vYwnmf',
                'log_id': '4c053da6vYwnmf'
            }
            data_val = '{"appKey":"444e9908a51d1cb236a27862abc769c9","deviceId":"' + self.device_id + '"}'
            data = {
                'data': data_val,
            }

            # 获取token
            token = None
            token = trans_cookies(self.cookies_str).get('_m_h5_tk', '').split('_')[0] if trans_cookies(self.cookies_str).get('_m_h5_tk') else ''

            sign = generate_sign(params['t'], token, data_val)
            params['sign'] = sign

            # 发送请求 - 使用与浏览器完全一致的请求头
            headers = {
                'accept': 'application/json',
                'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8',
                'cache-control': 'no-cache',
                'content-type': 'application/x-www-form-urlencoded',
                'pragma': 'no-cache',
                'priority': 'u=1, i',
                'sec-ch-ua': '"Not;A=Brand";v="99", "Google Chrome";v="139", "Chromium";v="139"',
                'sec-ch-ua-mobile': '?0',
                'sec-ch-ua-platform': '"Windows"',
                'sec-fetch-dest': 'empty',
                'sec-fetch-mode': 'cors',
                'sec-fetch-site': 'same-site',
                'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36',
                'referer': 'https://www.goofish.com/',
                'origin': 'https://www.goofish.com',
                'cookie': self.cookies_str
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    API_ENDPOINTS.get('token'),
                    params=params,
                    data=data,
                    headers=headers
                ) as response:
                    res_json = await response.json()

                    # 检查并更新Cookie
                    if 'set-cookie' in response.headers:
                        new_cookies = {}
                        for cookie in response.headers.getall('set-cookie', []):
                            if '=' in cookie:
                                name, value = cookie.split(';')[0].split('=', 1)
                                new_cookies[name.strip()] = value.strip()

                        # 更新cookies
                        if new_cookies:
                            self.cookies.update(new_cookies)
                            # 生成新的cookie字符串
                            self.cookies_str = '; '.join([f"{k}={v}" for k, v in self.cookies.items()])
                            # 更新数据库中的Cookie
                            await self.update_config_cookies()
                            logger.debug("已更新Cookie到数据库")

                    if isinstance(res_json, dict):
                        ret_value = res_json.get('ret', [])
                        # 检查ret是否包含成功信息
                        if any('SUCCESS::调用成功' in ret for ret in ret_value):
                            if 'data' in res_json and 'accessToken' in res_json['data']:
                                new_token = res_json['data']['accessToken']
                                self.current_token = new_token
                                self.last_token_refresh_time = time.time()

                                logger.info(f"【{self.cookie_id}】Token刷新成功")
                                return new_token

                    logger.error(f"【{self.cookie_id}】Token刷新失败: {res_json}")
                    self.token_failure_count += 1
                    logger.error(f"【{self.cookie_id}】Token刷新失败，累计失败次数: {self.token_failure_count}")

                    always_refresh = os.getenv("COOKIE_REFRESH_ALWAYS_ON_TOKEN_FAILURE", "false").lower() in ("true", "1", "t")
                    refresh_interval = int(os.getenv("COOKIE_REFRESH_TOKEN_FAILURE_INTERVAL", "60"))

                    should_refresh = False
                    if always_refresh:
                        should_refresh = True
                        logger.info(f"【{self.cookie_id}】策略为“每次失败都刷新”，触发强制刷新")
                    # 新逻辑：第一次失败时刷新，之后按间隔刷新
                    elif self.token_failure_count == 1:
                        should_refresh = True
                        logger.info(f"【{self.cookie_id}】首次 Token 刷新失败，触发强制刷新")
                    elif self.token_failure_count > 1 and (self.token_failure_count - 1) % refresh_interval == 0:
                        should_refresh = True
                        logger.info(f"【{self.cookie_id}】失败次数达到 {self.token_failure_count} (间隔 {refresh_interval})，触发强制刷新")

                    if should_refresh:
                        refreshed = await self._force_refresh_cookie()
                        if refreshed:
                            logger.info(f"【{self.cookie_id}】强制刷新 Cookie 成功，将使用新 Cookie 重新获取 Token")
                            self.token_failure_count = 0  # 刷新成功后重置计数器
                            return await self.refresh_token() # 立即使用新cookie重试
                        else:
                            logger.error(f"【{self.cookie_id}】强制刷新 Cookie 失败，将按原计划重试")

                    # 清空当前token，确保下次重试时重新获取
                    self.current_token = None

                    # 发送Token刷新失败通知
                    await self.send_token_refresh_notification(f"Token刷新失败: {res_json}", "token_refresh_failed")
                    return None

        except Exception as e:
            logger.error(f"Token刷新异常: {self._safe_str(e)}")

            # 清空当前token，确保下次重试时重新获取
            self.current_token = None

            # 发送Token刷新异常通知
            await self.send_token_refresh_notification(f"Token刷新异常: {str(e)}", "token_refresh_exception")
            return None
 
    async def _force_refresh_cookie(self):
        """
        强制刷新 Cookie 的兜底逻辑。
        - 优先从 CookieCloud 刷新。
        - 若 CookieCloud 失败或未配置，则尝试从浏览器刷新（如果支持）。
        """
        logger.warning(f"【{self.cookie_id}】触发强制刷新 Cookie 兜底机制")
        refreshed = False

        # 1. 尝试从 CookieCloud 刷新
        try:
            from utils.cookiecloud import fetch_cookiecloud_cookie_str
            host = (os.getenv("COOKIE_CLOUD_HOST") or "").strip()
            uuid = (os.getenv("COOKIE_CLOUD_UUID") or "").strip()
            password = (os.getenv("COOKIE_CLOUD_PASSWORD") or "").strip()

            if host and uuid and fetch_cookiecloud_cookie_str:
                logger.info(f"【{self.cookie_id}】尝试通过 CookieCloud 强制刷新 Cookie...")
                cookies_str = await fetch_cookiecloud_cookie_str(host, uuid, password, timeout=20)
                if cookies_str:
                    self.cookies_str = cookies_str
                    self.cookies = trans_cookies(cookies_str)
                    await self.update_config_cookies()
                    logger.info(f"【{self.cookie_id}】已通过 CookieCloud 强制刷新 Cookie")
                    refreshed = True
                else:
                    logger.warning(f"【{self.cookie_id}】CookieCloud 强制刷新失败")
        except Exception as e:
            logger.error(f"【{self.cookie_id}】CookieCloud 强制刷新异常: {e}")

        # 2. 若未成功，尝试从浏览器刷新 (此处为示例，实际需要实现)
        if not refreshed:
            logger.info(f"【{self.cookie_id}】尝试通过浏览器强制刷新 Cookie...")
            # 此处应调用 `cookie_manager` 中可能存在的浏览器刷新逻辑
            # from cookie_manager import manager as cookie_manager
            # success = await cookie_manager.refresh_cookie_from_browser(self.cookie_id)
            # if success:
            #     refreshed = True
            #     logger.info(f"【{self.cookie_id}】已通过浏览器强制刷新 Cookie")
            # else:
            #     logger.warning(f"【{self.cookie_id}】浏览器强制刷新失败")
            logger.warning(f"【{self.cookie_id}】未实现浏览器强制刷新逻辑，跳过")

        return refreshed

    async def update_config_cookies(self):
        """更新数据库中的cookies"""
        try:
            from db_manager import db_manager

            # 更新数据库中的Cookie
            if hasattr(self, 'cookie_id') and self.cookie_id:
                try:
                    # 获取当前Cookie的用户ID，避免在刷新时改变所有者
                    current_user_id = None
                    if hasattr(self, 'user_id') and self.user_id:
                        current_user_id = self.user_id

                    db_manager.save_cookie(self.cookie_id, self.cookies_str, current_user_id)
                    logger.debug(f"已更新Cookie到数据库: {self.cookie_id}")
                except Exception as e:
                    logger.error(f"更新数据库Cookie失败: {self._safe_str(e)}")
                    # 发送数据库更新失败通知
                    await self.send_token_refresh_notification(f"数据库Cookie更新失败: {str(e)}", "db_update_failed")
            else:
                logger.warning("Cookie ID不存在，无法更新数据库")
                # 发送Cookie ID缺失通知
                await self.send_token_refresh_notification("Cookie ID不存在，无法更新数据库", "cookie_id_missing")

        except Exception as e:
            logger.error(f"更新Cookie失败: {self._safe_str(e)}")
            # 发送Cookie更新失败通知
            await self.send_token_refresh_notification(f"Cookie更新失败: {str(e)}", "cookie_update_failed")

    async def _restart_instance(self):
        """重启XianyuLive实例"""
        try:
            logger.info(f"【{self.cookie_id}】Token刷新成功，准备重启实例...")

            # 导入CookieManager
            from cookie_manager import manager as cookie_manager

            if cookie_manager:
                # 通过CookieManager重启实例
                logger.info(f"【{self.cookie_id}】通过CookieManager重启实例...")

                # 使用异步方式调用update_cookie，避免阻塞
                def restart_task():
                    try:
                        cookie_manager.update_cookie(self.cookie_id, self.cookies_str)
                        logger.info(f"【{self.cookie_id}】实例重启请求已发送")
                    except Exception as e:
                        logger.error(f"【{self.cookie_id}】重启实例失败: {e}")

                # 在后台执行重启任务
                import threading
                restart_thread = threading.Thread(target=restart_task, daemon=True)
                restart_thread.start()

                logger.info(f"【{self.cookie_id}】实例重启已在后台执行")
            else:
                logger.warning(f"【{self.cookie_id}】CookieManager不可用，无法重启实例")

        except Exception as e:
            logger.error(f"【{self.cookie_id}】重启实例失败: {self._safe_str(e)}")
            # 发送重启失败通知
            await self.send_token_refresh_notification(f"实例重启失败: {str(e)}", "instance_restart_failed")

    async def save_item_info_to_db(self, item_id: str, item_detail: str = None, item_title: str = None):
        """保存商品信息到数据库

        Args:
            item_id: 商品ID
            item_detail: 商品详情内容（可以是任意格式的文本）
            item_title: 商品标题
        """
        try:
            # 跳过以 auto_ 开头的商品ID
            if item_id and item_id.startswith('auto_'):
                logger.debug(f"跳过保存自动生成的商品ID: {item_id}")
                return

            # 验证：如果只有商品ID，没有商品标题和商品详情，则不插入数据库
            if not item_title and not item_detail:
                logger.debug(f"跳过保存商品信息：缺少商品标题和详情 - {item_id}")
                return

            # 如果有商品标题但没有详情，也跳过（根据需求，需要同时有标题和详情）
            if not item_title or not item_detail:
                logger.debug(f"跳过保存商品信息：商品标题或详情不完整 - {item_id}")
                return

            from db_manager import db_manager

            # 直接使用传入的详情内容
            item_data = item_detail

            # 保存到数据库
            success = db_manager.save_item_info(self.cookie_id, item_id, item_data)
            if success:
                logger.info(f"商品信息已保存到数据库: {item_id}")
            else:
                logger.warning(f"保存商品信息到数据库失败: {item_id}")

        except Exception as e:
            logger.error(f"保存商品信息到数据库异常: {self._safe_str(e)}")

    async def save_item_detail_only(self, item_id, item_detail):
        """仅保存商品详情（不影响标题等基本信息）"""
        try:
            from db_manager import db_manager

            # 使用专门的详情更新方法
            success = db_manager.update_item_detail(self.cookie_id, item_id, item_detail)

            if success:
                logger.info(f"商品详情已更新: {item_id}")
            else:
                logger.warning(f"更新商品详情失败: {item_id}")

            return success

        except Exception as e:
            logger.error(f"更新商品详情异常: {self._safe_str(e)}")
            return False

    async def fetch_item_detail_from_api(self, item_id: str) -> str:
        """获取商品详情（优先使用浏览器，备用外部API，支持24小时缓存）

        Args:
            item_id: 商品ID

        Returns:
            str: 商品详情文本，获取失败返回空字符串
        """
        try:
            # 检查是否启用自动获取功能
            from config import config
            auto_fetch_config = config.get('ITEM_DETAIL', {}).get('auto_fetch', {})

            if not auto_fetch_config.get('enabled', True):
                logger.debug(f"自动获取商品详情功能已禁用: {item_id}")
                return ""

            # 1. 首先检查缓存（24小时有效）
            async with self._item_detail_cache_lock:
                if item_id in self._item_detail_cache:
                    cache_data = self._item_detail_cache[item_id]
                    cache_time = cache_data['timestamp']
                    current_time = time.time()

                    # 检查缓存是否在24小时内
                    if current_time - cache_time < 24 * 60 * 60:  # 24小时
                        logger.info(f"从缓存获取商品详情: {item_id}")
                        return cache_data['detail']
                    else:
                        # 缓存过期，删除
                        del self._item_detail_cache[item_id]
                        logger.debug(f"缓存已过期，删除: {item_id}")

            # 2. 尝试使用浏览器获取商品详情
            detail_from_browser = await self._fetch_item_detail_from_browser(item_id)
            if detail_from_browser:
                # 保存到缓存
                async with self._item_detail_cache_lock:
                    self._item_detail_cache[item_id] = {
                        'detail': detail_from_browser,
                        'timestamp': time.time()
                    }
                logger.info(f"成功通过浏览器获取商品详情: {item_id}, 长度: {len(detail_from_browser)}")
                return detail_from_browser

            # 3. 浏览器获取失败，使用外部API作为备用
            logger.warning(f"浏览器获取商品详情失败，尝试外部API: {item_id}")
            detail_from_api = await self._fetch_item_detail_from_external_api(item_id)
            if detail_from_api:
                # 保存到缓存
                async with self._item_detail_cache_lock:
                    self._item_detail_cache[item_id] = {
                        'detail': detail_from_api,
                        'timestamp': time.time()
                    }
                logger.info(f"成功通过外部API获取商品详情: {item_id}, 长度: {len(detail_from_api)}")
                return detail_from_api

            logger.warning(f"所有方式都无法获取商品详情: {item_id}")
            return ""

        except Exception as e:
            logger.error(f"获取商品详情异常: {item_id}, 错误: {self._safe_str(e)}")
            return ""

    async def _fetch_item_detail_from_browser(self, item_id: str) -> str:
        """使用浏览器获取商品详情"""
        try:
            from playwright.async_api import async_playwright

            logger.info(f"开始使用浏览器获取商品详情: {item_id}")

            playwright = await async_playwright().start()

            # 启动浏览器（参照order_detail_fetcher的配置）
            browser_args = [
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-accelerated-2d-canvas',
                '--no-first-run',
                '--no-zygote',
                '--disable-gpu',
                '--disable-background-timer-throttling',
                '--disable-backgrounding-occluded-windows',
                '--disable-renderer-backgrounding',
                '--disable-features=TranslateUI',
                '--disable-ipc-flooding-protection',
                '--disable-extensions',
                '--disable-default-apps',
                '--disable-sync',
                '--disable-translate',
                '--hide-scrollbars',
                '--mute-audio',
                '--no-default-browser-check',
                '--no-pings'
            ]

            # 在Docker环境中添加额外参数
            if os.getenv('DOCKER_ENV'):
                browser_args.extend([
                    '--single-process',
                    '--disable-background-networking',
                    '--disable-client-side-phishing-detection',
                    '--disable-hang-monitor',
                    '--disable-popup-blocking',
                    '--disable-prompt-on-repost',
                    '--disable-web-resources',
                    '--metrics-recording-only',
                    '--safebrowsing-disable-auto-update',
                    '--enable-automation',
                    '--password-store=basic',
                    '--use-mock-keychain'
                ])

            browser = await playwright.chromium.launch(
                headless=True,
                args=browser_args
            )

            # 创建浏览器上下文
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36'
            )

            # 设置Cookie
            cookies = []
            for cookie_pair in self.cookies_str.split('; '):
                if '=' in cookie_pair:
                    name, value = cookie_pair.split('=', 1)
                    cookies.append({
                        'name': name.strip(),
                        'value': value.strip(),
                        'domain': '.goofish.com',
                        'path': '/'
                    })

            await context.add_cookies(cookies)
            logger.debug(f"已设置 {len(cookies)} 个Cookie")

            # 创建页面
            page = await context.new_page()

            # 构造商品详情页面URL
            item_url = f"https://www.goofish.com/item?id={item_id}"
            logger.info(f"访问商品页面: {item_url}")

            # 访问页面
            await page.goto(item_url, wait_until='networkidle', timeout=30000)

            # 等待页面完全加载
            await asyncio.sleep(3)

            # 获取商品详情内容
            try:
                # 等待目标元素出现
                await page.wait_for_selector('.desc--GaIUKUQY', timeout=10000)

                # 获取商品详情文本
                detail_element = await page.query_selector('.desc--GaIUKUQY')
                if detail_element:
                    detail_text = await detail_element.inner_text()
                    logger.info(f"成功获取商品详情: {item_id}, 长度: {len(detail_text)}")

                    # 清理资源
                    await browser.close()
                    await playwright.stop()

                    return detail_text.strip()
                else:
                    logger.warning(f"未找到商品详情元素: {item_id}")

            except Exception as e:
                logger.warning(f"获取商品详情元素失败: {item_id}, 错误: {self._safe_str(e)}")

            # 清理资源
            await browser.close()
            await playwright.stop()

            return ""

        except Exception as e:
            logger.error(f"浏览器获取商品详情异常: {item_id}, 错误: {self._safe_str(e)}")
            return ""

    async def _fetch_item_detail_from_external_api(self, item_id: str) -> str:
        """从外部API获取商品详情（备用方案）"""
        try:
            from config import config
            auto_fetch_config = config.get('ITEM_DETAIL', {}).get('auto_fetch', {})

            # 从配置获取API地址和超时时间
            api_base_url = auto_fetch_config.get('api_url', 'https://selfapi.zhinianboke.com/api/getItemDetail')
            timeout_seconds = auto_fetch_config.get('timeout', 10)

            api_url = f"{api_base_url}/{item_id}"

            logger.info(f"正在从外部API获取商品详情: {item_id}")

            # 使用aiohttp发送异步请求
            import aiohttp

            timeout = aiohttp.ClientTimeout(total=timeout_seconds)

            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(api_url) as response:
                    if response.status == 200:
                        result = await response.json()

                        # 检查返回状态
                        if result.get('status') == '200' and result.get('data'):
                            item_detail = result['data']
                            logger.info(f"外部API成功获取商品详情: {item_id}, 长度: {len(item_detail)}")
                            return item_detail
                        else:
                            logger.warning(f"外部API返回状态异常: {result.get('status')}, message: {result.get('message')}")
                            return ""
                    else:
                        logger.warning(f"外部API请求失败: HTTP {response.status}")
                        return ""

        except asyncio.TimeoutError:
            logger.warning(f"外部API获取商品详情超时: {item_id}")
            return ""
        except Exception as e:
            logger.error(f"外部API获取商品详情异常: {item_id}, 错误: {self._safe_str(e)}")
            return ""

    async def save_items_list_to_db(self, items_list):
        """批量保存商品列表信息到数据库（并发安全）

        Args:
            items_list: 从get_item_list_info获取的商品列表
        """
        try:
            from db_manager import db_manager

            # 准备批量数据
            batch_data = []
            items_need_detail = []  # 需要获取详情的商品列表

            for item in items_list:
                item_id = item.get('id')
                if not item_id or item_id.startswith('auto_'):
                    continue

                # 构造商品详情数据
                item_detail = {
                    'title': item.get('title', ''),
                    'price': item.get('price', ''),
                    'price_text': item.get('price_text', ''),
                    'category_id': item.get('category_id', ''),
                    'auction_type': item.get('auction_type', ''),
                    'item_status': item.get('item_status', 0),
                    'detail_url': item.get('detail_url', ''),
                    'pic_info': item.get('pic_info', {}),
                    'detail_params': item.get('detail_params', {}),
                    'track_params': item.get('track_params', {}),
                    'item_label_data': item.get('item_label_data', {}),
                    'card_type': item.get('card_type', 0)
                }

                # 检查数据库中是否已有详情
                existing_item = db_manager.get_item_info(self.cookie_id, item_id)
                has_detail = existing_item and existing_item.get('item_detail') and existing_item['item_detail'].strip()

                batch_data.append({
                    'cookie_id': self.cookie_id,
                    'item_id': item_id,
                    'item_title': item.get('title', ''),
                    'item_description': '',  # 暂时为空
                    'item_category': str(item.get('category_id', '')),
                    'item_price': item.get('price_text', ''),
                    'item_detail': json.dumps(item_detail, ensure_ascii=False)
                })

                # 如果没有详情，添加到需要获取详情的列表
                if not has_detail:
                    items_need_detail.append({
                        'item_id': item_id,
                        'item_title': item.get('title', '')
                    })

            if not batch_data:
                logger.info("没有有效的商品数据需要保存")
                return 0

            # 使用批量保存方法（并发安全）
            saved_count = db_manager.batch_save_item_basic_info(batch_data)
            logger.info(f"批量保存商品信息完成: {saved_count}/{len(batch_data)} 个商品")

            # 异步获取缺失的商品详情
            if items_need_detail:
                from config import config
                auto_fetch_config = config.get('ITEM_DETAIL', {}).get('auto_fetch', {})

                if auto_fetch_config.get('enabled', True):
                    logger.info(f"发现 {len(items_need_detail)} 个商品缺少详情，开始获取...")
                    detail_success_count = await self._fetch_missing_item_details(items_need_detail)
                    logger.info(f"成功获取 {detail_success_count}/{len(items_need_detail)} 个商品的详情")
                else:
                    logger.info(f"发现 {len(items_need_detail)} 个商品缺少详情，但自动获取功能已禁用")

            return saved_count

        except Exception as e:
            logger.error(f"批量保存商品信息异常: {self._safe_str(e)}")
            return 0

    async def _fetch_missing_item_details(self, items_need_detail):
        """批量获取缺失的商品详情

        Args:
            items_need_detail: 需要获取详情的商品列表

        Returns:
            int: 成功获取详情的商品数量
        """
        success_count = 0

        try:
            from db_manager import db_manager
            from config import config

            # 从配置获取并发数量和延迟时间
            auto_fetch_config = config.get('ITEM_DETAIL', {}).get('auto_fetch', {})
            max_concurrent = auto_fetch_config.get('max_concurrent', 3)
            retry_delay = auto_fetch_config.get('retry_delay', 0.5)

            # 限制并发数量，避免对API服务器造成压力
            semaphore = asyncio.Semaphore(max_concurrent)

            async def fetch_single_item_detail(item_info):
                async with semaphore:
                    try:
                        item_id = item_info['item_id']
                        item_title = item_info['item_title']

                        # 获取商品详情
                        item_detail_text = await self.fetch_item_detail_from_api(item_id)

                        if item_detail_text:
                            # 保存详情到数据库
                            success = await self.save_item_detail_only(item_id, item_detail_text)
                            if success:
                                logger.info(f"✅ 成功获取并保存商品详情: {item_id} - {item_title}")
                                return 1
                            else:
                                logger.warning(f"❌ 获取详情成功但保存失败: {item_id}")
                        else:
                            logger.warning(f"❌ 未能获取商品详情: {item_id} - {item_title}")

                        # 添加延迟，避免请求过于频繁
                        await asyncio.sleep(retry_delay)
                        return 0

                    except Exception as e:
                        logger.error(f"获取单个商品详情异常: {item_info.get('item_id', 'unknown')}, 错误: {self._safe_str(e)}")
                        return 0

            # 并发获取所有商品详情
            tasks = [fetch_single_item_detail(item_info) for item_info in items_need_detail]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # 统计成功数量
            for result in results:
                if isinstance(result, int):
                    success_count += result
                elif isinstance(result, Exception):
                    logger.error(f"获取商品详情任务异常: {result}")

            return success_count

        except Exception as e:
            logger.error(f"批量获取商品详情异常: {self._safe_str(e)}")
            return success_count

    async def get_item_info(self, item_id, retry_count=0):
        """获取商品信息，自动处理token失效的情况"""
        if retry_count >= 4:  # 最多重试3次
            logger.error("获取商品信息失败，重试次数过多")
            return {"error": "获取商品信息失败，重试次数过多"}

        # 确保session已创建
        if not self.session:
            await self.create_session()

        params = {
            'jsv': '2.7.2',
            'appKey': '34839810',
            't': str(int(time.time()) * 1000),
            'sign': '',
            'v': '1.0',
            'type': 'originaljson',
            'accountSite': 'xianyu',
            'dataType': 'json',
            'timeout': '20000',
            'api': 'mtop.taobao.idle.pc.detail',
            'sessionOption': 'AutoLoginOnly',
            'spm_cnt': 'a21ybx.im.0.0',
        }

        data_val = '{"itemId":"' + item_id + '"}'
        data = {
            'data': data_val,
        }

        # 始终从最新的cookies中获取_m_h5_tk token（刷新后cookies会被更新）
        token = trans_cookies(self.cookies_str).get('_m_h5_tk', '').split('_')[0] if trans_cookies(self.cookies_str).get('_m_h5_tk') else ''

        if token:
            logger.debug(f"使用cookies中的_m_h5_tk token: {token}")
        else:
            logger.warning("cookies中没有找到_m_h5_tk token")

        from utils.xianyu_utils import generate_sign
        sign = generate_sign(params['t'], token, data_val)
        params['sign'] = sign

        try:
            async with self.session.post(
                'https://h5api.m.goofish.com/h5/mtop.taobao.idle.pc.detail/1.0/',
                params=params,
                data=data
            ) as response:
                res_json = await response.json()

                # 检查并更新Cookie
                if 'set-cookie' in response.headers:
                    new_cookies = {}
                    for cookie in response.headers.getall('set-cookie', []):
                        if '=' in cookie:
                            name, value = cookie.split(';')[0].split('=', 1)
                            new_cookies[name.strip()] = value.strip()

                    # 更新cookies
                    if new_cookies:
                        self.cookies.update(new_cookies)
                        # 生成新的cookie字符串
                        self.cookies_str = '; '.join([f"{k}={v}" for k, v in self.cookies.items()])
                        # 更新数据库中的Cookie
                        await self.update_config_cookies()
                        logger.debug("已更新Cookie到数据库")

                logger.debug(f"商品信息获取成功: {res_json}")
                # 检查返回状态
                if isinstance(res_json, dict):
                    ret_value = res_json.get('ret', [])
                    # 检查ret是否包含成功信息
                    if not any('SUCCESS::调用成功' in ret for ret in ret_value):
                        logger.warning(f"商品信息API调用失败，错误信息: {ret_value}")

                        await asyncio.sleep(0.5)
                        return await self.get_item_info(item_id, retry_count + 1)
                    else:
                        logger.debug(f"商品信息获取成功: {item_id}")
                        return res_json
                else:
                    logger.error(f"商品信息API返回格式异常: {res_json}")
                    return await self.get_item_info(item_id, retry_count + 1)

        except Exception as e:
            logger.error(f"商品信息API请求异常: {self._safe_str(e)}")
            await asyncio.sleep(0.5)
            return await self.get_item_info(item_id, retry_count + 1)

    def extract_item_id_from_message(self, message):
        """从消息中提取商品ID的辅助方法"""
        try:
            # 方法1: 从message["1"]中提取（如果是字符串格式）
            message_1 = message.get('1')
            if isinstance(message_1, str):
                # 尝试从字符串中提取数字ID
                id_match = re.search(r'(\d{10,})', message_1)
                if id_match:
                    logger.info(f"从message[1]字符串中提取商品ID: {id_match.group(1)}")
                    return id_match.group(1)

            # 方法2: 从message["3"]中提取
            message_3 = message.get('3', {})
            if isinstance(message_3, dict):

                # 从extension中提取
                if 'extension' in message_3:
                    extension = message_3['extension']
                    if isinstance(extension, dict):
                        item_id = extension.get('itemId') or extension.get('item_id')
                        if item_id:
                            logger.info(f"从extension中提取商品ID: {item_id}")
                            return item_id

                # 从bizData中提取
                if 'bizData' in message_3:
                    biz_data = message_3['bizData']
                    if isinstance(biz_data, dict):
                        item_id = biz_data.get('itemId') or biz_data.get('item_id')
                        if item_id:
                            logger.info(f"从bizData中提取商品ID: {item_id}")
                            return item_id

                # 从其他可能的字段中提取
                for key, value in message_3.items():
                    if isinstance(value, dict):
                        item_id = value.get('itemId') or value.get('item_id')
                        if item_id:
                            logger.info(f"从{key}字段中提取商品ID: {item_id}")
                            return item_id

                # 从消息内容中提取数字ID
                content = message_3.get('content', '')
                if isinstance(content, str) and content:
                    id_match = re.search(r'(\d{10,})', content)
                    if id_match:
                        logger.info(f"【{self.cookie_id}】从消息内容中提取商品ID: {id_match.group(1)}")
                        return id_match.group(1)

            # 方法3: 遍历整个消息结构查找可能的商品ID
            def find_item_id_recursive(obj, path=""):
                if isinstance(obj, dict):
                    # 直接查找itemId字段
                    for key in ['itemId', 'item_id', 'id']:
                        if key in obj and isinstance(obj[key], (str, int)):
                            value = str(obj[key])
                            if len(value) >= 10 and value.isdigit():
                                logger.info(f"从{path}.{key}中提取商品ID: {value}")
                                return value

                    # 递归查找
                    for key, value in obj.items():
                        result = find_item_id_recursive(value, f"{path}.{key}" if path else key)
                        if result:
                            return result

                elif isinstance(obj, str):
                    # 从字符串中提取可能的商品ID
                    id_match = re.search(r'(\d{10,})', obj)
                    if id_match:
                        logger.info(f"从{path}字符串中提取商品ID: {id_match.group(1)}")
                        return id_match.group(1)

                return None

            result = find_item_id_recursive(message)
            if result:
                return result

            logger.debug("所有方法都未能提取到商品ID")
            return None

        except Exception as e:
            logger.error(f"提取商品ID失败: {self._safe_str(e)}")
            return None

    def debug_message_structure(self, message, context=""):
        """调试消息结构的辅助方法"""
        try:
            logger.debug(f"[{context}] 消息结构调试:")
            logger.debug(f"  消息类型: {type(message)}")

            if isinstance(message, dict):
                for key, value in message.items():
                    logger.debug(f"  键 '{key}': {type(value)} - {str(value)[:100]}...")

                    # 特别关注可能包含商品ID的字段
                    if key in ["1", "3"] and isinstance(value, dict):
                        logger.debug(f"    详细结构 '{key}':")
                        for sub_key, sub_value in value.items():
                            logger.debug(f"      '{sub_key}': {type(sub_value)} - {str(sub_value)[:50]}...")
            else:
                logger.debug(f"  消息内容: {str(message)[:200]}...")

        except Exception as e:
            logger.error(f"调试消息结构时发生错误: {self._safe_str(e)}")

    async def get_default_reply(self, send_user_name: str, send_user_id: str, send_message: str, chat_id: str, item_id: str = None) -> str:
        """获取默认回复内容，支持指定商品回复、变量替换和只回复一次功能"""
        try:
            from db_manager import db_manager

            # 1. 优先检查指定商品回复
            if item_id:
                item_reply = db_manager.get_item_reply(self.cookie_id, item_id)
                if item_reply and item_reply.get('reply_content'):
                    reply_content = item_reply['reply_content']
                    logger.info(f"【{self.cookie_id}】使用指定商品回复: 商品ID={item_id}")

                    # 进行变量替换
                    try:
                        formatted_reply = reply_content.format(
                            send_user_name=send_user_name,
                            send_user_id=send_user_id,
                            send_message=send_message,
                            item_id=item_id
                        )
                        logger.info(f"【{self.cookie_id}】指定商品回复内容: {formatted_reply}")
                        return formatted_reply
                    except Exception as format_error:
                        logger.error(f"指定商品回复变量替换失败: {self._safe_str(format_error)}")
                        # 如果变量替换失败，返回原始内容
                        return reply_content
                else:
                    logger.debug(f"【{self.cookie_id}】商品ID {item_id} 没有配置指定回复，使用默认回复")

            # 2. 获取当前账号的默认回复设置
            default_reply_settings = db_manager.get_default_reply(self.cookie_id)

            if not default_reply_settings or not default_reply_settings.get('enabled', False):
                logger.debug(f"账号 {self.cookie_id} 未启用默认回复")
                return None

            # 检查"只回复一次"功能
            if default_reply_settings.get('reply_once', False) and chat_id:
                # 检查是否已经回复过这个chat_id
                if db_manager.has_default_reply_record(self.cookie_id, chat_id):
                    logger.info(f"【{self.cookie_id}】chat_id {chat_id} 已使用过默认回复，跳过（只回复一次）")
                    return None

            reply_content = default_reply_settings.get('reply_content', '')
            if not reply_content or (reply_content and reply_content.strip() == ''):
                logger.info(f"账号 {self.cookie_id} 默认回复内容为空，不进行回复")
                return "EMPTY_REPLY"  # 返回特殊标记表示不回复

            # 进行变量替换
            try:
                # 获取当前商品是否有设置自动回复
                item_replay = db_manager.get_item_replay(item_id)

                formatted_reply = reply_content.format(
                    send_user_name=send_user_name,
                    send_user_id=send_user_id,
                    send_message=send_message
                )

                if item_replay:
                    formatted_reply = item_replay.get('reply_content', '')

                # 如果开启了"只回复一次"功能，记录这次回复
                if default_reply_settings.get('reply_once', False) and chat_id:
                    db_manager.add_default_reply_record(self.cookie_id, chat_id)
                    logger.info(f"【{self.cookie_id}】记录默认回复: chat_id={chat_id}")

                logger.info(f"【{self.cookie_id}】使用默认回复: {formatted_reply}")
                return formatted_reply
            except Exception as format_error:
                logger.error(f"默认回复变量替换失败: {self._safe_str(format_error)}")
                # 如果变量替换失败，返回原始内容
                return reply_content

        except Exception as e:
            logger.error(f"获取默认回复失败: {self._safe_str(e)}")
            return None

    async def get_keyword_reply(self, send_user_name: str, send_user_id: str, send_message: str, item_id: str = None) -> str:
        """获取关键词匹配回复（支持商品ID优先匹配和图片类型）"""
        try:
            from db_manager import db_manager

            # 获取当前账号的关键词列表（包含类型信息）
            keywords = db_manager.get_keywords_with_type(self.cookie_id)

            if not keywords:
                logger.debug(f"账号 {self.cookie_id} 没有配置关键词")
                return None

            # 1. 如果有商品ID，优先匹配该商品ID对应的关键词
            if item_id:
                for keyword_data in keywords:
                    keyword = keyword_data['keyword']
                    reply = keyword_data['reply']
                    keyword_item_id = keyword_data['item_id']
                    keyword_type = keyword_data.get('type', 'text')
                    image_url = keyword_data.get('image_url')

                    if keyword_item_id == item_id and keyword.lower() in send_message.lower():
                        logger.info(f"商品ID关键词匹配成功: 商品{item_id} '{keyword}' (类型: {keyword_type})")

                        # 根据关键词类型处理
                        if keyword_type == 'image' and image_url:
                            # 图片类型关键词，发送图片
                            return await self._handle_image_keyword(keyword, image_url, send_user_name, send_user_id, send_message)
                        else:
                            # 文本类型关键词，检查回复内容是否为空
                            if not reply or (reply and reply.strip() == ''):
                                logger.info(f"商品ID关键词 '{keyword}' 回复内容为空，不进行回复")
                                return "EMPTY_REPLY"  # 返回特殊标记表示匹配到但不回复

                            # 进行变量替换
                            try:
                                formatted_reply = reply.format(
                                    send_user_name=send_user_name,
                                    send_user_id=send_user_id,
                                    send_message=send_message
                                )
                                logger.info(f"商品ID文本关键词回复: {formatted_reply}")
                                return formatted_reply
                            except Exception as format_error:
                                logger.error(f"关键词回复变量替换失败: {self._safe_str(format_error)}")
                                # 如果变量替换失败，返回原始内容
                                return reply

            # 2. 如果商品ID匹配失败或没有商品ID，匹配没有商品ID的通用关键词
            for keyword_data in keywords:
                keyword = keyword_data['keyword']
                reply = keyword_data['reply']
                keyword_item_id = keyword_data['item_id']
                keyword_type = keyword_data.get('type', 'text')
                image_url = keyword_data.get('image_url')

                if not keyword_item_id and keyword.lower() in send_message.lower():
                    logger.info(f"通用关键词匹配成功: '{keyword}' (类型: {keyword_type})")

                    # 根据关键词类型处理
                    if keyword_type == 'image' and image_url:
                        # 图片类型关键词，发送图片
                        return await self._handle_image_keyword(keyword, image_url, send_user_name, send_user_id, send_message)
                    else:
                        # 文本类型关键词，检查回复内容是否为空
                        if not reply or (reply and reply.strip() == ''):
                            logger.info(f"通用关键词 '{keyword}' 回复内容为空，不进行回复")
                            return "EMPTY_REPLY"  # 返回特殊标记表示匹配到但不回复

                        # 进行变量替换
                        try:
                            formatted_reply = reply.format(
                                send_user_name=send_user_name,
                                send_user_id=send_user_id,
                                send_message=send_message
                            )
                            logger.info(f"通用文本关键词回复: {formatted_reply}")
                            return formatted_reply
                        except Exception as format_error:
                            logger.error(f"关键词回复变量替换失败: {self._safe_str(format_error)}")
                            # 如果变量替换失败，返回原始内容
                            return reply

            logger.debug(f"未找到匹配的关键词: {send_message}")
            return None

        except Exception as e:
            logger.error(f"获取关键词回复失败: {self._safe_str(e)}")
            return None

    async def _handle_image_keyword(self, keyword: str, image_url: str, send_user_name: str, send_user_id: str, send_message: str) -> str:
        """处理图片类型关键词"""
        try:
            # 检查图片URL类型
            if self._is_cdn_url(image_url):
                # 已经是CDN链接，直接使用
                logger.info(f"使用已有的CDN图片链接: {image_url}")
                return f"__IMAGE_SEND__{image_url}"

            elif image_url.startswith('/static/uploads/') or image_url.startswith('static/uploads/'):
                # 本地图片，需要上传到闲鱼CDN
                local_image_path = image_url.replace('/static/uploads/', 'static/uploads/')
                if os.path.exists(local_image_path):
                    logger.info(f"准备上传本地图片到闲鱼CDN: {local_image_path}")

                    # 使用图片上传器上传到闲鱼CDN
                    from utils.image_uploader import ImageUploader
                    uploader = ImageUploader(self.cookies_str)

                    async with uploader:
                        cdn_url = await uploader.upload_image(local_image_path)
                        if cdn_url:
                            logger.info(f"图片上传成功，CDN URL: {cdn_url}")
                            # 更新数据库中的图片URL为CDN URL
                            await self._update_keyword_image_url(keyword, cdn_url)
                            image_url = cdn_url
                        else:
                            logger.error(f"图片上传失败: {local_image_path}")
                            return f"抱歉，图片发送失败，请稍后重试。"
                else:
                    logger.error(f"本地图片文件不存在: {local_image_path}")
                    return f"抱歉，图片文件不存在。"

            else:
                # 其他类型的URL（可能是外部链接），直接使用
                logger.info(f"使用外部图片链接: {image_url}")

            # 发送图片（这里返回特殊标记，在调用处处理实际发送）
            return f"__IMAGE_SEND__{image_url}"

        except Exception as e:
            logger.error(f"处理图片关键词失败: {e}")
            return f"抱歉，图片发送失败: {str(e)}"

    def _is_cdn_url(self, url: str) -> bool:
        """检查URL是否是闲鱼CDN链接"""
        if not url:
            return False

        # 闲鱼CDN域名列表
        cdn_domains = [
            'gw.alicdn.com',
            'img.alicdn.com',
            'cloud.goofish.com',
            'goofish.com',
            'taobaocdn.com',
            'tbcdn.cn',
            'aliimg.com'
        ]

        # 检查是否包含CDN域名
        url_lower = url.lower()
        for domain in cdn_domains:
            if domain in url_lower:
                return True

        # 检查是否是HTTPS链接且包含图片特征
        if url_lower.startswith('https://') and any(ext in url_lower for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']):
            return True

        return False

    async def _update_keyword_image_url(self, keyword: str, new_image_url: str):
        """更新关键词的图片URL"""
        try:
            from db_manager import db_manager
            success = db_manager.update_keyword_image_url(self.cookie_id, keyword, new_image_url)
            if success:
                logger.info(f"图片URL已更新: {keyword} -> {new_image_url}")
            else:
                logger.warning(f"图片URL更新失败: {keyword}")
        except Exception as e:
            logger.error(f"更新关键词图片URL失败: {e}")

    async def _update_card_image_url(self, card_id: int, new_image_url: str):
        """更新卡券的图片URL"""
        try:
            from db_manager import db_manager
            success = db_manager.update_card_image_url(card_id, new_image_url)
            if success:
                logger.info(f"卡券图片URL已更新: 卡券ID={card_id} -> {new_image_url}")
            else:
                logger.warning(f"卡券图片URL更新失败: 卡券ID={card_id}")
        except Exception as e:
            logger.error(f"更新卡券图片URL失败: {e}")

    async def get_ai_reply(self, send_user_name: str, send_user_id: str, send_message: str, item_id: str, chat_id: str):
        """获取AI回复"""
        try:
            from ai_reply_engine import ai_reply_engine

            # 检查是否启用AI回复
            if not ai_reply_engine.is_ai_enabled(self.cookie_id):
                logger.debug(f"账号 {self.cookie_id} 未启用AI回复")
                return None

            # 从数据库获取商品信息
            from db_manager import db_manager
            item_info_raw = db_manager.get_item_info(self.cookie_id, item_id)

            if not item_info_raw:
                logger.debug(f"数据库中无商品信息: {item_id}")
                # 使用默认商品信息
                item_info = {
                    'title': '商品信息获取失败',
                    'price': 0,
                    'desc': '暂无商品描述'
                }
            else:
                # 解析数据库中的商品信息
                item_info = {
                    'title': item_info_raw.get('item_title', '未知商品'),
                    'price': self._parse_price(item_info_raw.get('item_price', '0')),
                    'desc': item_info_raw.get('item_description', '暂无商品描述')
                }

            # 生成AI回复
            reply = ai_reply_engine.generate_reply(
                message=send_message,
                item_info=item_info,
                chat_id=chat_id,
                cookie_id=self.cookie_id,
                user_id=send_user_id,
                item_id=item_id
            )

            if reply:
                logger.info(f"【{self.cookie_id}】AI回复生成成功: {reply}")
                return reply
            else:
                logger.debug(f"AI回复生成失败")
                return None

        except Exception as e:
            logger.error(f"获取AI回复失败: {self._safe_str(e)}")
            return None

    def _parse_price(self, price_str: str) -> float:
        """解析价格字符串为数字"""
        try:
            if not price_str:
                return 0.0
            # 移除非数字字符，保留小数点
            price_clean = re.sub(r'[^\d.]', '', str(price_str))
            return float(price_clean) if price_clean else 0.0
        except:
            return 0.0

    async def send_notification(self, send_user_name: str, send_user_id: str, send_message: str, item_id: str = None, chat_id: str = None):
        """发送消息通知"""
        try:
            from db_manager import db_manager
            import aiohttp

            # 过滤系统默认消息，不发送通知
            system_messages = [
                '发来一条消息',
                '发来一条新消息'
            ]

            if send_message in system_messages:
                logger.debug(f"📱 系统消息不发送通知: {send_message}")
                return

            logger.info(f"📱 开始发送消息通知 - 账号: {self.cookie_id}, 买家: {send_user_name}")

            # 获取当前账号的通知配置
            notifications = db_manager.get_account_notifications(self.cookie_id)

            if not notifications:
                logger.warning(f"📱 账号 {self.cookie_id} 未配置消息通知，跳过通知发送")
                return

            logger.info(f"📱 找到 {len(notifications)} 个通知渠道配置")

            # 构建通知消息
            notification_msg = f"🚨 接收消息通知\n\n" \
                             f"账号: {self.cookie_id}\n" \
                             f"买家: {send_user_name} (ID: {send_user_id})\n" \
                             f"商品ID: {item_id or '未知'}\n" \
                             f"聊天ID: {chat_id or '未知'}\n" \
                             f"消息内容: {send_message}\n" \
                             f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"

            # 发送通知到各个渠道
            for i, notification in enumerate(notifications, 1):
                logger.info(f"📱 处理第 {i} 个通知渠道: {notification.get('channel_name', 'Unknown')}")

                if not notification.get('enabled', True):
                    logger.warning(f"📱 通知渠道 {notification.get('channel_name')} 已禁用，跳过")
                    continue

                channel_type = notification.get('channel_type')
                channel_config = notification.get('channel_config')

                logger.info(f"📱 渠道类型: {channel_type}, 配置: {channel_config}")

                try:
                    # 解析配置数据
                    config_data = self._parse_notification_config(channel_config)
                    logger.info(f"📱 解析后的配置数据: {config_data}")

                    match channel_type:
                        case 'qq':
                            logger.info(f"📱 开始发送QQ通知...")
                            await self._send_qq_notification(config_data, notification_msg)
                        case 'ding_talk' | 'dingtalk':
                            logger.info(f"📱 开始发送钉钉通知...")
                            await self._send_dingtalk_notification(config_data, notification_msg)
                        case 'feishu' | 'lark':
                            logger.info(f"📱 开始发送飞书通知...")
                            await self._send_feishu_notification(config_data, notification_msg)
                        case 'bark':
                            logger.info(f"📱 开始发送Bark通知...")
                            await self._send_bark_notification(config_data, notification_msg)
                        case 'email':
                            logger.info(f"📱 开始发送邮件通知...")
                            await self._send_email_notification(config_data, notification_msg)
                        case 'webhook':
                            logger.info(f"📱 开始发送Webhook通知...")
                            await self._send_webhook_notification(config_data, notification_msg)
                        case 'wechat':
                            logger.info(f"📱 开始发送微信通知...")
                            await self._send_wechat_notification(config_data, notification_msg)
                        case 'telegram':
                            logger.info(f"📱 开始发送Telegram通知...")
                            await self._send_telegram_notification(config_data, notification_msg)
                        case _:
                            logger.warning(f"📱 不支持的通知渠道类型: {channel_type}")

                except Exception as notify_error:
                    logger.error(f"📱 发送通知失败 ({notification.get('channel_name', 'Unknown')}): {self._safe_str(notify_error)}")
                    import traceback
                    logger.error(f"📱 详细错误信息: {traceback.format_exc()}")

        except Exception as e:
            logger.error(f"📱 处理消息通知失败: {self._safe_str(e)}")
            import traceback
            logger.error(f"📱 详细错误信息: {traceback.format_exc()}")

    def _parse_notification_config(self, config: str) -> dict:
        """解析通知配置数据"""
        try:
            import json
            # 尝试解析JSON格式的配置
            return json.loads(config)
        except (json.JSONDecodeError, TypeError):
            # 兼容旧格式（直接字符串）
            return {"config": config}

    async def _send_qq_notification(self, config_data: dict, message: str):
        """发送QQ通知"""
        try:
            import aiohttp

            logger.info(f"📱 QQ通知 - 开始处理配置数据: {config_data}")

            # 解析配置（QQ号码）
            qq_number = config_data.get('qq_number') or config_data.get('config', '')
            qq_number = qq_number.strip() if qq_number else ''

            logger.info(f"📱 QQ通知 - 解析到QQ号码: {qq_number}")

            if not qq_number:
                logger.warning("📱 QQ通知 - QQ号码配置为空，无法发送通知")
                return

            # 构建请求URL
            api_url = "http://notice.zhinianblog.cn/sendPrivateMsg"
            params = {
                'qq': qq_number,
                'msg': message
            }

            logger.info(f"📱 QQ通知 - 请求URL: {api_url}")
            logger.info(f"📱 QQ通知 - 请求参数: qq={qq_number}, msg长度={len(message)}")

            # 发送GET请求
            async with aiohttp.ClientSession() as session:
                async with session.get(api_url, params=params, timeout=10) as response:
                    response_text = await response.text()
                    logger.info(f"📱 QQ通知 - 响应状态: {response.status}")
                    logger.info(f"📱 QQ通知 - 响应内容: {response_text}")

                    if response.status == 200:
                        logger.info(f"📱 QQ通知发送成功: {qq_number}")
                    else:
                        logger.warning(f"📱 QQ通知发送失败: HTTP {response.status}, 响应: {response_text}")

        except Exception as e:
            logger.error(f"📱 发送QQ通知异常: {self._safe_str(e)}")
            import traceback
            logger.error(f"📱 QQ通知异常详情: {traceback.format_exc()}")

    async def _send_dingtalk_notification(self, config_data: dict, message: str):
        """发送钉钉通知"""
        try:
            import aiohttp
            import json
            import hmac
            import hashlib
            import base64
            import time

            # 解析配置
            webhook_url = config_data.get('webhook_url') or config_data.get('config', '')
            secret = config_data.get('secret', '')

            webhook_url = webhook_url.strip() if webhook_url else ''
            if not webhook_url:
                logger.warning("钉钉通知配置为空")
                return

            # 如果有加签密钥，生成签名
            if secret:
                timestamp = str(round(time.time() * 1000))
                secret_enc = secret.encode('utf-8')
                string_to_sign = f'{timestamp}\n{secret}'
                string_to_sign_enc = string_to_sign.encode('utf-8')
                hmac_code = hmac.new(secret_enc, string_to_sign_enc, digestmod=hashlib.sha256).digest()
                sign = base64.b64encode(hmac_code).decode('utf-8')
                webhook_url += f'&timestamp={timestamp}&sign={sign}'

            data = {
                "msgtype": "markdown",
                "markdown": {
                    "title": "闲鱼自动回复通知",
                    "text": message
                }
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(webhook_url, json=data, timeout=10) as response:
                    if response.status == 200:
                        logger.info(f"钉钉通知发送成功")
                    else:
                        logger.warning(f"钉钉通知发送失败: {response.status}")

        except Exception as e:
            logger.error(f"发送钉钉通知异常: {self._safe_str(e)}")

    async def _send_feishu_notification(self, config_data: dict, message: str):
        """发送飞书通知"""
        try:
            import aiohttp
            import json
            import hmac
            import hashlib
            import base64

            logger.info(f"📱 飞书通知 - 开始处理配置数据: {config_data}")

            # 解析配置
            webhook_url = config_data.get('webhook_url', '')
            secret = config_data.get('secret', '')

            logger.info(f"📱 飞书通知 - Webhook URL: {webhook_url[:50]}...")
            logger.info(f"📱 飞书通知 - 是否有签名密钥: {'是' if secret else '否'}")

            if not webhook_url:
                logger.warning("📱 飞书通知 - Webhook URL配置为空，无法发送通知")
                return

            # 如果有加签密钥，生成签名
            timestamp = str(int(time.time()))
            sign = ""

            if secret:
                string_to_sign = f'{timestamp}\n{secret}'
                hmac_code = hmac.new(
                    secret.encode('utf-8'),
                    string_to_sign.encode('utf-8'),
                    digestmod=hashlib.sha256
                ).digest()
                sign = base64.b64encode(hmac_code).decode('utf-8')
                logger.info(f"📱 飞书通知 - 已生成签名")

            # 构建请求数据
            data = {
                "msg_type": "text",
                "content": {
                    "text": message
                },
                "timestamp": timestamp
            }

            # 如果有签名，添加到请求数据中
            if sign:
                data["sign"] = sign

            logger.info(f"📱 飞书通知 - 请求数据构建完成")

            # 发送POST请求
            async with aiohttp.ClientSession() as session:
                async with session.post(webhook_url, json=data, timeout=10) as response:
                    response_text = await response.text()
                    logger.info(f"📱 飞书通知 - 响应状态: {response.status}")
                    logger.info(f"📱 飞书通知 - 响应内容: {response_text}")

                    if response.status == 200:
                        try:
                            response_json = json.loads(response_text)
                            if response_json.get('code') == 0:
                                logger.info(f"📱 飞书通知发送成功")
                            else:
                                logger.warning(f"📱 飞书通知发送失败: {response_json.get('msg', '未知错误')}")
                        except json.JSONDecodeError:
                            logger.info(f"📱 飞书通知发送成功（响应格式异常）")
                    else:
                        logger.warning(f"📱 飞书通知发送失败: HTTP {response.status}, 响应: {response_text}")

        except Exception as e:
            logger.error(f"📱 发送飞书通知异常: {self._safe_str(e)}")
            import traceback
            logger.error(f"📱 飞书通知异常详情: {traceback.format_exc()}")

    async def _send_bark_notification(self, config_data: dict, message: str):
        """发送Bark通知"""
        try:
            import aiohttp
            import json
            from urllib.parse import quote

            logger.info(f"📱 Bark通知 - 开始处理配置数据: {config_data}")

            # 解析配置
            server_url = config_data.get('server_url', 'https://api.day.app').rstrip('/')
            device_key = config_data.get('device_key', '')
            title = config_data.get('title', '闲鱼自动回复通知')
            sound = config_data.get('sound', 'default')
            icon = config_data.get('icon', '')
            group = config_data.get('group', 'xianyu')
            url = config_data.get('url', '')

            logger.info(f"📱 Bark通知 - 服务器: {server_url}")
            logger.info(f"📱 Bark通知 - 设备密钥: {device_key[:10]}..." if device_key else "📱 Bark通知 - 设备密钥: 未设置")
            logger.info(f"📱 Bark通知 - 标题: {title}")

            if not device_key:
                logger.warning("📱 Bark通知 - 设备密钥配置为空，无法发送通知")
                return

            # 构建请求URL和数据
            # Bark支持两种方式：URL路径方式和POST JSON方式
            # 这里使用POST JSON方式，更灵活且支持更多参数

            api_url = f"{server_url}/push"

            # 构建请求数据
            data = {
                "device_key": device_key,
                "title": title,
                "body": message,
                "sound": sound,
                "group": group
            }

            # 可选参数
            if icon:
                data["icon"] = icon
            if url:
                data["url"] = url

            logger.info(f"📱 Bark通知 - API地址: {api_url}")
            logger.info(f"📱 Bark通知 - 请求数据构建完成")

            # 发送POST请求
            async with aiohttp.ClientSession() as session:
                async with session.post(api_url, json=data, timeout=10) as response:
                    response_text = await response.text()
                    logger.info(f"📱 Bark通知 - 响应状态: {response.status}")
                    logger.info(f"📱 Bark通知 - 响应内容: {response_text}")

                    if response.status == 200:
                        try:
                            response_json = json.loads(response_text)
                            if response_json.get('code') == 200:
                                logger.info(f"📱 Bark通知发送成功")
                            else:
                                logger.warning(f"📱 Bark通知发送失败: {response_json.get('message', '未知错误')}")
                        except json.JSONDecodeError:
                            # 某些Bark服务器可能返回纯文本
                            if 'success' in response_text.lower() or 'ok' in response_text.lower():
                                logger.info(f"📱 Bark通知发送成功")
                            else:
                                logger.warning(f"📱 Bark通知响应格式异常: {response_text}")
                    else:
                        logger.warning(f"📱 Bark通知发送失败: HTTP {response.status}, 响应: {response_text}")

        except Exception as e:
            logger.error(f"📱 发送Bark通知异常: {self._safe_str(e)}")
            import traceback
            logger.error(f"📱 Bark通知异常详情: {traceback.format_exc()}")

    async def _send_email_notification(self, config_data: dict, message: str):
        """发送邮件通知"""
        try:
            import smtplib
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart

            # 解析配置
            smtp_server = config_data.get('smtp_server', '')
            smtp_port = int(config_data.get('smtp_port', 587))
            email_user = config_data.get('email_user', '')
            email_password = config_data.get('email_password', '')
            recipient_email = config_data.get('recipient_email', '')

            if not all([smtp_server, email_user, email_password, recipient_email]):
                logger.warning("邮件通知配置不完整")
                return

            # 创建邮件
            msg = MIMEMultipart()
            msg['From'] = email_user
            msg['To'] = recipient_email
            msg['Subject'] = "闲鱼自动回复通知"

            # 添加邮件正文
            msg.attach(MIMEText(message, 'plain', 'utf-8'))

            # 发送邮件
            server = smtplib.SMTP(smtp_server, smtp_port)
            server.starttls()
            server.login(email_user, email_password)
            server.send_message(msg)
            server.quit()

            logger.info(f"邮件通知发送成功: {recipient_email}")

        except Exception as e:
            logger.error(f"发送邮件通知异常: {self._safe_str(e)}")

    async def _send_webhook_notification(self, config_data: dict, message: str):
        """发送Webhook通知"""
        try:
            import aiohttp
            import json

            # 解析配置
            webhook_url = config_data.get('webhook_url', '')
            http_method = config_data.get('http_method', 'POST').upper()
            headers_str = config_data.get('headers', '{}')

            if not webhook_url:
                logger.warning("Webhook通知配置为空")
                return

            # 解析自定义请求头
            try:
                custom_headers = json.loads(headers_str) if headers_str else {}
            except json.JSONDecodeError:
                custom_headers = {}

            # 设置默认请求头
            headers = {'Content-Type': 'application/json'}
            headers.update(custom_headers)

            # 构建请求数据
            data = {
                'message': message,
                'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
                'source': 'xianyu-auto-reply'
            }

            async with aiohttp.ClientSession() as session:
                if http_method == 'POST':
                    async with session.post(webhook_url, json=data, headers=headers, timeout=10) as response:
                        if response.status == 200:
                            logger.info(f"Webhook通知发送成功")
                        else:
                            logger.warning(f"Webhook通知发送失败: {response.status}")
                elif http_method == 'PUT':
                    async with session.put(webhook_url, json=data, headers=headers, timeout=10) as response:
                        if response.status == 200:
                            logger.info(f"Webhook通知发送成功")
                        else:
                            logger.warning(f"Webhook通知发送失败: {response.status}")
                else:
                    logger.warning(f"不支持的HTTP方法: {http_method}")

        except Exception as e:
            logger.error(f"发送Webhook通知异常: {self._safe_str(e)}")

    async def _send_wechat_notification(self, config_data: dict, message: str):
        """发送微信通知"""
        try:
            import aiohttp
            import json

            # 解析配置
            webhook_url = config_data.get('webhook_url', '')

            if not webhook_url:
                logger.warning("微信通知配置为空")
                return

            data = {
                "msgtype": "text",
                "text": {
                    "content": message
                }
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(webhook_url, json=data, timeout=10) as response:
                    if response.status == 200:
                        logger.info(f"微信通知发送成功")
                    else:
                        logger.warning(f"微信通知发送失败: {response.status}")

        except Exception as e:
            logger.error(f"发送微信通知异常: {self._safe_str(e)}")

    async def _send_telegram_notification(self, config_data: dict, message: str):
        """发送Telegram通知"""
        try:
            import aiohttp

            # 解析配置
            bot_token = config_data.get('bot_token', '')
            chat_id = config_data.get('chat_id', '')

            if not all([bot_token, chat_id]):
                logger.warning("Telegram通知配置不完整")
                return

            # 构建API URL
            api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

            data = {
                'chat_id': chat_id,
                'text': message,
                'parse_mode': 'HTML'
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(api_url, json=data, timeout=10) as response:
                    if response.status == 200:
                        logger.info(f"Telegram通知发送成功")
                    else:
                        logger.warning(f"Telegram通知发送失败: {response.status}")

        except Exception as e:
            logger.error(f"发送Telegram通知异常: {self._safe_str(e)}")

    async def send_token_refresh_notification(self, error_message: str, notification_type: str = "token_refresh", chat_id: str = None):
        """发送Token刷新异常通知（带防重复机制）"""
        try:
            # 检查是否是正常的令牌过期，这种情况不需要发送通知
            if self._is_normal_token_expiry(error_message):
                logger.debug(f"检测到正常的令牌过期，跳过通知: {error_message}")
                return

            # 检查是否在冷却期内
            current_time = time.time()
            last_time = self.last_notification_time.get(notification_type, 0)

            # 为Token刷新异常通知使用特殊的3小时冷却时间
            # 基于错误消息内容判断是否为Token相关异常
            if self._is_token_related_error(error_message):
                cooldown_time = self.token_refresh_notification_cooldown
                cooldown_desc = "3小时"
            else:
                cooldown_time = self.notification_cooldown
                cooldown_desc = f"{self.notification_cooldown // 60}分钟"

            if current_time - last_time < cooldown_time:
                remaining_time = cooldown_time - (current_time - last_time)
                remaining_hours = int(remaining_time // 3600)
                remaining_minutes = int((remaining_time % 3600) // 60)
                remaining_seconds = int(remaining_time % 60)

                if remaining_hours > 0:
                    time_desc = f"{remaining_hours}小时{remaining_minutes}分钟"
                elif remaining_minutes > 0:
                    time_desc = f"{remaining_minutes}分钟{remaining_seconds}秒"
                else:
                    time_desc = f"{remaining_seconds}秒"

                logger.debug(f"Token刷新通知在冷却期内，跳过发送: {notification_type} (还需等待 {time_desc})")
                return

            from db_manager import db_manager

            # 获取当前账号的通知配置
            notifications = db_manager.get_account_notifications(self.cookie_id)

            if not notifications:
                logger.debug("未配置消息通知，跳过Token刷新通知")
                return

            # 构造通知消息
            notification_msg = f"""🔴 闲鱼账号Token刷新异常

账号ID: {self.cookie_id}
聊天ID: {chat_id or '未知'}
异常时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}
异常信息: {error_message}

请检查账号Cookie是否过期，如有需要请及时更新Cookie配置。"""

            logger.info(f"准备发送Token刷新异常通知: {self.cookie_id}")

            # 发送通知到各个渠道
            notification_sent = False
            for notification in notifications:
                if not notification.get('enabled', True):
                    continue

                channel_type = notification.get('channel_type')
                channel_config = notification.get('channel_config')

                try:
                    # 解析配置数据
                    config_data = self._parse_notification_config(channel_config)

                    match channel_type:
                        case 'qq':
                            await self._send_qq_notification(config_data, notification_msg)
                            notification_sent = True
                        case 'ding_talk' | 'dingtalk':
                            await self._send_dingtalk_notification(config_data, notification_msg)
                            notification_sent = True
                        case 'feishu' | 'lark':
                            await self._send_feishu_notification(config_data, notification_msg)
                            notification_sent = True
                        case 'bark':
                            await self._send_bark_notification(config_data, notification_msg)
                            notification_sent = True
                        case 'email':
                            await self._send_email_notification(config_data, notification_msg)
                            notification_sent = True
                        case 'webhook':
                            await self._send_webhook_notification(config_data, notification_msg)
                            notification_sent = True
                        case 'wechat':
                            await self._send_wechat_notification(config_data, notification_msg)
                            notification_sent = True
                        case 'telegram':
                            await self._send_telegram_notification(config_data, notification_msg)
                            notification_sent = True
                        case _:
                            logger.warning(f"不支持的通知渠道类型: {channel_type}")

                except Exception as notify_error:
                    logger.error(f"发送Token刷新通知失败 ({notification.get('channel_name', 'Unknown')}): {self._safe_str(notify_error)}")

            # 如果成功发送了通知，更新最后发送时间
            if notification_sent:
                self.last_notification_time[notification_type] = current_time

                # 根据错误消息内容使用不同的冷却时间
                if self._is_token_related_error(error_message):
                    next_send_time = current_time + self.token_refresh_notification_cooldown
                    cooldown_desc = "3小时"
                else:
                    next_send_time = current_time + self.notification_cooldown
                    cooldown_desc = f"{self.notification_cooldown // 60}分钟"

                next_send_time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(next_send_time))
                logger.info(f"Token刷新通知已发送，下次可发送时间: {next_send_time_str} (冷却时间: {cooldown_desc})")

        except Exception as e:
            logger.error(f"处理Token刷新通知失败: {self._safe_str(e)}")

    def _is_normal_token_expiry(self, error_message: str) -> bool:
        """检查是否是正常的令牌过期或其他不需要通知的情况"""
        # 不需要发送通知的关键词
        no_notification_keywords = [
            # 正常的令牌过期
            'FAIL_SYS_TOKEN_EXOIRED::令牌过期',
            'FAIL_SYS_TOKEN_EXPIRED::令牌过期',
            'FAIL_SYS_TOKEN_EXOIRED',
            'FAIL_SYS_TOKEN_EXPIRED',
            '令牌过期',
            # Session过期（正常情况）
            'FAIL_SYS_SESSION_EXPIRED::Session过期',
            'FAIL_SYS_SESSION_EXPIRED',
            'Session过期',
            # Token定时刷新失败（会自动重试）
            'Token定时刷新失败，将自动重试',
            'Token定时刷新失败'
        ]

        # 检查错误消息是否包含不需要通知的关键词
        for keyword in no_notification_keywords:
            if keyword in error_message:
                return True

        return False

    def _is_token_related_error(self, error_message: str) -> bool:
        """检查是否是Token相关的错误，需要使用3小时冷却时间"""
        # Token相关错误的关键词
        token_error_keywords = [
            # Token刷新失败相关
            'Token刷新失败',
            'Token刷新异常',
            'token刷新失败',
            'token刷新异常',
            'TOKEN刷新失败',
            'TOKEN刷新异常',
            # 具体的Token错误信息
            'FAIL_SYS_USER_VALIDATE',
            'RGV587_ERROR',
            '哎哟喂,被挤爆啦',
            '请稍后重试',
            'punish?x5secdata',
            'captcha',
            # Token获取失败
            '无法获取有效token',
            '无法获取有效Token',
            'Token获取失败',
            'token获取失败',
            'TOKEN获取失败',
            # Token定时刷新失败
            'Token定时刷新失败',
            'token定时刷新失败',
            'TOKEN定时刷新失败',
            # 初始化Token失败
            '初始化时无法获取有效Token',
            '初始化时无法获取有效token',
            # 其他Token相关错误
            'accessToken',
            'access_token',
            '_m_h5_tk',
            'mtop.taobao.idlemessage.pc.login.token'
        ]

        # 检查错误消息是否包含Token相关的关键词
        error_message_lower = error_message.lower()
        for keyword in token_error_keywords:
            if keyword.lower() in error_message_lower:
                return True

        return False

    async def send_delivery_failure_notification(self, send_user_name: str, send_user_id: str, item_id: str, error_message: str, chat_id: str = None):
        """发送自动发货失败通知"""
        try:
            from db_manager import db_manager

            # 获取当前账号的通知配置
            notifications = db_manager.get_account_notifications(self.cookie_id)

            if not notifications:
                logger.debug("未配置消息通知，跳过自动发货失败通知")
                return

            # 构造通知消息
            notification_msg = f"""⚠️ 自动发货异常通知

账号ID: {self.cookie_id}
买家: {send_user_name} (ID: {send_user_id})
商品ID: {item_id or '未知'}
聊天ID: {chat_id or '未知'}
异常时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}
异常信息: {error_message}

请关注并及时处理。"""

            # 发送通知到各个渠道
            for notification in notifications:
                if not notification.get('enabled', True):
                    continue

                channel_type = notification.get('channel_type')
                channel_config = notification.get('channel_config')

                try:
                    # 解析配置数据
                    config_data = self._parse_notification_config(channel_config)

                    match channel_type:
                        case 'qq':
                            await self._send_qq_notification(config_data, notification_msg)
                        case 'ding_talk' | 'dingtalk':
                            await self._send_dingtalk_notification(config_data, notification_msg)
                        case 'feishu' | 'lark':
                            await self._send_feishu_notification(config_data, notification_msg)
                        case 'bark':
                            await self._send_bark_notification(config_data, notification_msg)
                        case 'email':
                            await self._send_email_notification(config_data, notification_msg)
                        case 'webhook':
                            await self._send_webhook_notification(config_data, notification_msg)
                        case 'wechat':
                            await self._send_wechat_notification(config_data, notification_msg)
                        case 'telegram':
                            await self._send_telegram_notification(config_data, notification_msg)
                        case _:
                            logger.warning(f"不支持的通知渠道类型: {channel_type}")

                except Exception as notify_error:
                    logger.error(f"发送自动发货失败通知失败 ({notification.get('channel_name', 'Unknown')}): {self._safe_str(notify_error)}")

        except Exception as e:
            logger.error(f"处理自动发货失败通知失败: {self._safe_str(e)}")
