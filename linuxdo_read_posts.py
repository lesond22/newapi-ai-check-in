#!/usr/bin/env python3
"""
使用 Camoufox 登录 Linux.do 并浏览帖子
"""

import asyncio
import hashlib
import json
import os
import re
import sys
import random
from datetime import datetime
from dotenv import load_dotenv
from camoufox.async_api import AsyncCamoufox
from utils.browser_utils import take_screenshot, save_page_content_to_file
from utils.notify import notify
from utils.mask_utils import mask_username

# 默认缓存目录，与 checkin.py 保持一致
DEFAULT_STORAGE_STATE_DIR = "storage-states"

# 帖子起始 ID，从环境变量获取，默认 随机从100000-1100000选一个
DEFAULT_BASE_TOPIC_ID = random.randint(1000000, 1100000)

# 帖子 ID 缓存目录
TOPIC_ID_CACHE_DIR = "linuxdo_reads"


class LinuxDoReadPosts:
    """Linux.do 帖子浏览类"""

    def __init__(
        self,
        username: str,
        password: str,
        storage_state_dir: str = DEFAULT_STORAGE_STATE_DIR,
    ):
        self.username = username
        self.password = password
        self.masked_username = mask_username(username)
        self.storage_state_dir = storage_state_dir
        self.username_hash = hashlib.sha256(username.encode("utf-8")).hexdigest()[:8]

        os.makedirs(self.storage_state_dir, exist_ok=True)
        os.makedirs(TOPIC_ID_CACHE_DIR, exist_ok=True)

        self.topic_id_cache_file = os.path.join(TOPIC_ID_CACHE_DIR, f"{self.username_hash}_topic_id.txt")

    async def _is_logged_in(self, page) -> bool:
        try:
            print(f"ℹ️ {self.masked_username}: Checking login status...")
            await page.goto("https://linux.do/", wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            current_url = page.url
            print(f"ℹ️ {self.masked_username}: Current URL: {current_url}")

            if current_url.startswith("https://linux.do/login"):
                print(f"ℹ️ {self.masked_username}: Redirected to login page, not logged in")
                return False

            print(f"✅ {self.masked_username}: Already logged in")
            return True
        except Exception as e:
            print(f"⚠️ {self.masked_username}: Error checking login status: {e}")
            return False

    async def _do_login(self, page) -> bool:
        try:
            print(f"ℹ️ {self.masked_username}: Starting login process...")

            if not page.url.startswith("https://linux.do/login"):
                await page.goto("https://linux.do/login", wait_until="domcontentloaded")

            await page.wait_for_timeout(2000)
            await page.fill("#login-account-name", self.username)
            await page.wait_for_timeout(2000)
            await page.fill("#login-account-password", self.password)
            await page.wait_for_timeout(2000)
            await page.click("#login-button")
            await page.wait_for_timeout(10000)

            await save_page_content_to_file(page, "login_result", self.username)

            current_url = page.url
            print(f"ℹ️ {self.masked_username}: URL after login: {current_url}")

            if "linux.do/challenge" in current_url:
                print(
                    f"⚠️ {self.masked_username}: Cloudflare challenge detected, "
                    "Camoufox should bypass it automatically. Waiting..."
                )
                try:
                    await page.wait_for_url("https://linux.do/", timeout=60000)
                    print(f"✅ {self.masked_username}: Cloudflare challenge bypassed")
                except Exception:
                    print(f"⚠️ {self.masked_username}: Cloudflare challenge timeout")

            current_url = page.url
            if current_url.startswith("https://linux.do/login"):
                print(f"❌ {self.masked_username}: Login failed, still on login page")
                await take_screenshot(page, "login_failed", self.username)
                return False

            print(f"✅ {self.masked_username}: Login successful")
            return True

        except Exception as e:
            print(f"❌ {self.masked_username}: Error during login: {e}")
            await take_screenshot(page, "login_error", self.username)
            return False

    def _load_topic_id(self) -> int:
        try:
            if os.path.exists(self.topic_id_cache_file):
                with open(self.topic_id_cache_file, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        return int(content)
                    else:
                        print(f"⚠️ {self.masked_username}: Failed to load topic ID from cache, content is empty")
        except (ValueError, IOError) as e:
            print(f"⚠️ {self.masked_username}: Failed to load topic ID from cache: {e}")
        return 0

    def _save_topic_id(self, topic_id: int) -> None:
        try:
            with open(self.topic_id_cache_file, "w", encoding="utf-8") as f:
                f.write(str(topic_id))
            print(f"ℹ️ {self.masked_username}: Saved topic ID {topic_id} to cache")
        except IOError as e:
            print(f"⚠️ {self.masked_username}: Failed to save topic ID: {e}")

    async def _get_topic_ids_from_latest(self, page) -> list:
        """从 latest.json 获取有效的帖子 ID 列表"""
        try:
            print(f"ℹ️ {self.masked_username}: Fetching topic IDs from latest.json...")
            await page.goto("https://linux.do/latest.json", wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            # 获取页面内容
            body = await page.query_selector('body')
            if not body:
                print(f"⚠️ {self.masked_username}: Cannot find body element")
                return []

            json_str = await body.inner_text()

            # 尝试解析 JSON
            data = json.loads(json_str)

            topic_ids = []
            if 'topic_list' in data and 'topics' in data['topic_list']:
                for topic in data['topic_list']['topics']:
                    if 'id' in topic:
                        topic_ids.append(topic['id'])

            print(f"✅ {self.masked_username}: Got {len(topic_ids)} topic IDs from latest.json")
            return topic_ids

        except json.JSONDecodeError as e:
            print(f"⚠️ {self.masked_username}: Failed to parse latest.json: {e}")
            return []
        except Exception as e:
            print(f"⚠️ {self.masked_username}: Error fetching latest.json: {e}")
            return []

    async def _scroll_to_read(self, page, max_scrolls: int = 5) -> int:
        """
        模拟阅读帖子：随机滚动几次
        返回实际滚动的次数
        """
        # 随机决定滚动次数（3-max_scrolls次）
        scroll_count = random.randint(3, max_scrolls)
        actual_scrolls = 0

        for i in range(scroll_count):
            # 随机滚动距离（0.5-1.5 个屏幕高度）
            scroll_ratio = random.uniform(0.5, 1.5)
            await page.evaluate(f"window.scrollBy(0, window.innerHeight * {scroll_ratio})")
            actual_scrolls += 1

            # 随机等待 2-5 秒，模拟阅读
            wait_time = random.randint(2000, 5000)
            await page.wait_for_timeout(wait_time)

            # 检查是否已经到底部
            at_bottom = await page.evaluate(
                "(window.innerHeight + window.scrollY) >= document.body.scrollHeight - 100"
            )
            if at_bottom:
                print(f"ℹ️ {self.masked_username}: Reached bottom after {actual_scrolls} scrolls")
                break

        return actual_scrolls

    async def _read_posts_from_list(self, page, topic_ids: list, max_posts: int) -> tuple[int, int]:
        """从给定的帖子 ID 列表中阅读帖子"""
        # 随机打乱顺序
        random.shuffle(topic_ids)

        read_count = 0
        last_topic_id = 0

        for topic_id in topic_ids:
            if read_count >= max_posts:
                break

            topic_url = f"https://linux.do/t/topic/{topic_id}"

            try:
                print(f"ℹ️ {self.masked_username}: Opening topic {topic_id}...")
                await page.goto(topic_url, wait_until="domcontentloaded")
                await page.wait_for_timeout(3000)

                timeline_element = await page.query_selector(".timeline-replies")

                if timeline_element:
                    inner_text = await timeline_element.inner_text()
                    print(f"✅ {self.masked_username}: Topic {topic_id} - Progress: {inner_text.strip()}")

                    # 模拟阅读：滚动几次
                    scrolls = await self._scroll_to_read(page)
                    print(f"ℹ️ {self.masked_username}: Scrolled {scrolls} times")

                    # 每个帖子计数 1
                    read_count += 1
                    last_topic_id = topic_id
                    print(f"ℹ️ {self.masked_username}: {read_count}/{max_posts} topics read")

                    # 帖子之间额外等待 2-5 秒
                    await page.wait_for_timeout(random.randint(2000, 5000))
                else:
                    print(f"⚠️ {self.masked_username}: Topic {topic_id} not accessible, skipping...")

            except Exception as e:
                print(f"⚠️ {self.masked_username}: Error reading topic {topic_id}: {e}")

        return last_topic_id, read_count

    async def _read_posts_sequential(self, page, base_topic_id: int, max_posts: int) -> tuple[int, int]:
        """顺序遍历模式（fallback）"""
        cached_topic_id = self._load_topic_id()
        current_topic_id = max(base_topic_id, cached_topic_id)
        print(
            f"ℹ️ {self.masked_username}: [Fallback] Starting from topic ID {current_topic_id} "
            f"(base: {base_topic_id}, cached: {cached_topic_id})"
        )

        read_count = 0
        invalid_count = 0

        while read_count < max_posts:
            if invalid_count >= 5:
                jump = random.randint(50, 100)
                current_topic_id += jump
                print(f"⚠️ {self.masked_username}: Too many invalid topics, jumping ahead by {jump} to {current_topic_id}")
                invalid_count = 0
            else:
                current_topic_id += random.randint(1, 5)

            topic_url = f"https://linux.do/t/topic/{current_topic_id}"

            try:
                print(f"ℹ️ {self.masked_username}: Opening topic {current_topic_id}...")
                await page.goto(topic_url, wait_until="domcontentloaded")
                await page.wait_for_timeout(3000)

                timeline_element = await page.query_selector(".timeline-replies")

                if timeline_element:
                    inner_text = await timeline_element.inner_text()
                    print(f"✅ {self.masked_username}: Topic {current_topic_id} - Progress: {inner_text.strip()}")

                    invalid_count = 0

                    # 模拟阅读：滚动几次
                    scrolls = await self._scroll_to_read(page)
                    print(f"ℹ️ {self.masked_username}: Scrolled {scrolls} times")

                    # 每个帖子计数 1
                    read_count += 1
                    print(f"ℹ️ {self.masked_username}: {read_count}/{max_posts} topics read")

                    # 帖子之间额外等待 2-5 秒
                    await page.wait_for_timeout(random.randint(2000, 5000))
                else:
                    print(f"⚠️ {self.masked_username}: Topic {current_topic_id} not found or invalid, skipping...")
                    invalid_count += 1

            except Exception as e:
                print(f"⚠️ {self.masked_username}: Error reading topic {current_topic_id}: {e}")
                invalid_count += 1

        self._save_topic_id(current_topic_id)

        return current_topic_id, read_count

    async def run(self, max_posts: int = 100) -> tuple[bool, dict]:
        print(f"ℹ️ {self.masked_username}: Starting Linux.do read posts task")

        cache_file_path = f"{self.storage_state_dir}/linuxdo_{self.username_hash}_storage_state.json"

        base_topic_id_str = os.getenv("LINUXDO_BASE_TOPIC_ID", "")
        base_topic_id = int(base_topic_id_str) if base_topic_id_str else DEFAULT_BASE_TOPIC_ID

        async with AsyncCamoufox(
            headless=False,
            humanize=True,
            locale="en-US",
        ) as browser:
            storage_state = cache_file_path if os.path.exists(cache_file_path) else None
            if storage_state:
                print(f"ℹ️ {self.masked_username}: Restoring storage state from cache")
            else:
                print(f"ℹ️ {self.masked_username}: No cache file found, starting fresh")

            context = await browser.new_context(storage_state=storage_state)
            page = await context.new_page()

            try:
                is_logged_in = await self._is_logged_in(page)

                if not is_logged_in:
                    login_success = await self._do_login(page)
                    if not login_success:
                        return False, {"error": "Login failed"}

                    await context.storage_state(path=cache_file_path)
                    print(f"✅ {self.masked_username}: Storage state saved to cache file")

                print(f"ℹ️ {self.masked_username}: Starting to read posts...")

                # 优先从 latest.json 获取帖子 ID
                topic_ids = []
                try:
                    topic_ids = await self._get_topic_ids_from_latest(page)
                except Exception as e:
                    print(f"⚠️ {self.masked_username}: Failed to get topic IDs: {e}")

                if topic_ids and len(topic_ids) > 0:
                    # 使用 latest.json 获取的帖子列表
                    last_topic_id, read_count = await self._read_posts_from_list(page, topic_ids, max_posts)
                else:
                    # Fallback 到顺序遍历模式
                    print(f"⚠️ {self.masked_username}: Falling back to sequential mode...")
                    last_topic_id, read_count = await self._read_posts_sequential(page, base_topic_id, max_posts)

                print(f"✅ {self.masked_username}: Successfully read {read_count} topics")
                return True, {
                    "read_count": read_count,
                    "last_topic_id": last_topic_id,
                }

            except Exception as e:
                print(f"❌ {self.masked_username}: Error occurred: {e}")
                await take_screenshot(page, "error", self.username)
                return False, {"error": str(e)}
            finally:
                await page.close()
                await context.close()


def load_linuxdo_accounts() -> list[dict]:
    accounts_str = os.getenv("ACCOUNTS")
    if not accounts_str:
        print("❌ ACCOUNTS environment variable not found")
        return []

    try:
        accounts_data = json.loads(accounts_str)

        if not isinstance(accounts_data, list):
            print("❌ ACCOUNTS must be a JSON array")
            return []

        linuxdo_accounts = []
        seen_usernames = set()

        for i, account in enumerate(accounts_data):
            if not isinstance(account, dict):
                print(f"⚠️ ACCOUNTS[{i}] must be a dictionary, skipping")
                continue

            username = account.get("username")
            masked_username = mask_username(username)
            password = account.get("password")

            if not username or not password:
                print(f"⚠️ ACCOUNTS[{i}] missing username or password, skipping")
                continue

            if username in seen_usernames:
                print(f"ℹ️ Skipping duplicate account: {masked_username}")
                continue

            seen_usernames.add(username)
            linuxdo_accounts.append(
                {
                    "username": username,
                    "password": password,
                }
            )

        return linuxdo_accounts

    except json.JSONDecodeError as e:
        print(f"❌ Failed to parse ACCOUNTS: {e}")
        return []
    except Exception as e:
        print(f"❌ Error loading ACCOUNTS: {e}")
        return []


async def main():
    load_dotenv(override=True)

    print("🚀 Linux.do read posts script started")
    print(f'🕒 Execution time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')

    accounts = load_linuxdo_accounts()

    if not accounts:
        print("❌ No accounts with linux.do configuration found")
        return

    print(f"ℹ️ Found {len(accounts)} account(s) with linux.do configuration")

    results = []

    for account in accounts:
        username = account["username"]
        masked_username = mask_username(username)
        password = account["password"]

        print(f"\n{'='*50}")
        print(f"📌 Processing: {masked_username}")
        print(f"{'='*50}")

        try:
            reader = LinuxDoReadPosts(
                username=username,
                password=password,
            )

            start_time = datetime.now()
            # 每次阅读 10-20 个帖子
            success, result = await reader.run(random.randint(10, 20))
            end_time = datetime.now()
            duration = end_time - start_time

            total_seconds = int(duration.total_seconds())
            hours, remainder = divmod(total_seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            duration_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

            print(f"Result: success={success}, result={result}, duration={duration_str}")

            results.append(
                {
                    "username": username,
                    "success": success,
                    "result": result,
                    "duration": duration_str,
                }
            )
        except Exception as e:
            print(f"❌ {masked_username}: Exception occurred: {e}")
            results.append(
                {
                    "username": username,
                    "success": False,
                    "result": {"error": str(e)},
                    "duration": "00:00:00",
                }
            )

    if results:
        notification_lines = [
            f'🕒 Execution time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
            "",
        ]

        total_read_count = 0
        for r in results:
            username = r["username"]
            masked_username = mask_username(username)
            duration = r["duration"]
            if r["success"]:
                read_count = r["result"].get("read_count", 0)
                total_read_count += read_count
                last_topic_id = r["result"].get("last_topic_id", "unknown")
                topic_url = f"https://linux.do/t/topic/{last_topic_id}"
                notification_lines.append(
                    f"✅ {masked_username}: Read {read_count} topics ({duration})\n" f"   Last topic: {topic_url}"
                )
            else:
                error = r["result"].get("error", "Unknown error")
                notification_lines.append(f"❌ {masked_username}: {error} ({duration})")

        notification_lines.append("")
        notification_lines.append(f"📊 Total read: {total_read_count} topics")

        notify_content = "\n".join(notification_lines)
        notify.push_message("Linux.do Read Posts", notify_content, msg_type="text")


def run_main():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⚠️ Program interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Error occurred during program execution: {e}")
        sys.exit(1)


if __name__ == "__main__":
    run_main()
