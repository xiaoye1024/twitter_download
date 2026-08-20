"""
批量获取推特用户帖子和媒体文件
支持：推文文本、图片、视频下载
"""

import asyncio
import json
import os
import sys
import re
import traceback
from datetime import datetime
from pathlib import Path

import aiohttp
import aiofiles
from twikit import Client


class TwitterScraper:
    def __init__(self, config_path="config.json"):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)

        # 环境变量覆盖敏感配置（用于 GitHub Actions 等 CI 环境）
        account = self.config["twitter_account"]
        if os.environ.get("TWITTER_USERNAME"):
            account["username"] = os.environ["TWITTER_USERNAME"]
        if os.environ.get("TWITTER_EMAIL"):
            account["email"] = os.environ["TWITTER_EMAIL"]
        if os.environ.get("TWITTER_PASSWORD"):
            account["password"] = os.environ["TWITTER_PASSWORD"]
        if os.environ.get("TARGET_USERS"):
            self.config["target_users"] = os.environ["TARGET_USERS"].split(",")
        if os.environ.get("MAX_TWEETS"):
            self.config["max_tweets"] = int(os.environ["MAX_TWEETS"])

        proxy = os.environ.get("TWITTER_PROXY") or self.config.get("proxy")
        self.client = Client(language="zh-cn", proxy=proxy)
        self.output_dir = Path(self.config.get("output_dir", "output"))
        self.media_dir = Path(self.config.get("media_dir", "media"))
        self.cookies_file = Path(self.config.get("cookies_file", "cookies.json"))
        self.output_dir.mkdir(exist_ok=True)
        self.media_dir.mkdir(exist_ok=True)

    def _check_config(self):
        """检查配置是否有效"""
        account = self.config.get("twitter_account", {})
        username = account.get("username", "")
        password = account.get("password", "")
        target_users = self.config.get("target_users", [])

        errors = []

        if username in ("", "your_twitter_username"):
            errors.append("请先在 config.json 中填写你的推特用户名 (twitter_account.username)")
        if password in ("", "your_password"):
            errors.append("请先在 config.json 中填写你的推特密码 (twitter_account.password)")
        if not target_users or target_users == ["elonmusk"] and username == "your_twitter_username":
            errors.append("请先在 config.json 中填写要抓取的目标用户 (target_users)")

        if errors:
            print("\n[!] 配置检查失败：")
            for err in errors:
                print(f"    - {err}")
            print("\n请编辑 config.json 文件，填入真实信息后重新运行。\n")
            return False
        return True

    async def login(self):
        """登录推特，优先使用 cookies 文件"""
        account = self.config["twitter_account"]

        # 尝试加载 cookies
        if self.cookies_file.exists():
            print(f"[*] 从 {self.cookies_file} 加载 cookies...")
            try:
                self.client.load_cookies(str(self.cookies_file))
                print("[+] Cookies 加载成功，验证登录状态...")
                user = await self.client.user()
                print(f"[+] 已登录为: @{user.screen_name}")
                return True
            except Exception as e:
                print(f"[*] Cookies 无效: {e}")
                print("[*] 将使用账号密码重新登录...")

        # 使用账号密码登录
        print(f"[*] 正在登录 @{account['username']}...")
        try:
            await self.client.login(
                auth_info_1=account["username"],
                auth_info_2=account.get("email", ""),
                password=account["password"],
                cookies_file=str(self.cookies_file),
            )
            print(f"[+] 登录成功! cookies 已保存到 {self.cookies_file}")
            return True
        except Exception as e:
            print(f"[-] 登录失败!")
            print(f"    错误类型: {type(e).__name__}")
            print(f"    错误信息: {e}")
            print(f"    详细堆栈:\n{traceback.format_exc()}")
            print(f"\n[!] 常见原因：")
            print(f"    1. 账号或密码错误")
            print(f"    2. 账号开启了双重验证(2FA)，需要在 config.json 中添加 totp_secret")
            print(f"    3. 推特要求验证码，请稍后再试或换用 cookies 方式登录")
            return False

    async def get_user_tweets(self, username: str, max_tweets: int = 100):
        """获取指定用户的所有推文"""
        print(f"\n[*] 正在获取 @{username} 的推文 (最多 {max_tweets} 条)...")

        try:
            user = await self.client.get_user_by_screen_name(username)
        except Exception as e:
            print(f"[-] 获取用户 @{username} 失败: {e}")
            print(f"    详细堆栈:\n{traceback.format_exc()}")
            return []

        tweets_data = []
        tweet_count = 0

        try:
            user_tweets = await user.get_tweets("Tweets", count=min(max_tweets, 40))

            while tweet_count < max_tweets:
                for tweet in user_tweets:
                    if tweet_count >= max_tweets:
                        break

                    tweet_data = self._parse_tweet(tweet, username)
                    tweets_data.append(tweet_data)

                    tweet_count += 1
                    if tweet_count % 10 == 0:
                        print(f"  已获取 {tweet_count} 条推文...")

                # 获取更多推文
                if tweet_count < max_tweets:
                    try:
                        user_tweets = await user_tweets.next()
                    except Exception:
                        break

            print(f"[+] @{username} 获取完成，共 {len(tweets_data)} 条推文")
            return tweets_data

        except Exception as e:
            print(f"[-] 获取推文时出错: {e}")
            print(f"    详细堆栈:\n{traceback.format_exc()}")
            return tweets_data

    def _parse_tweet(self, tweet, username: str) -> dict:
        """解析推文数据"""
        tweet_data = {
            "id": tweet.id,
            "url": f"https://x.com/{username}/status/{tweet.id}",
            "text": tweet.text,
            "created_at": str(tweet.created_at) if tweet.created_at else "",
            "user": username,
            "retweet_count": getattr(tweet, "retweet_count", 0),
            "favorite_count": getattr(tweet, "favorite_count", 0),
            "reply_count": getattr(tweet, "reply_count", 0),
            "view_count": getattr(tweet, "view_count", 0),
            "lang": getattr(tweet, "lang", ""),
            "media": [],
        }

        # 提取媒体文件
        if hasattr(tweet, "media") and tweet.media:
            for media_item in tweet.media:
                media_info = {
                    "type": media_item.get("type", "photo"),
                    "url": media_item.get("media_url_https", ""),
                    "preview_url": media_item.get("media_url_https", ""),
                }
                tweet_data["media"].append(media_info)

        return tweet_data

    async def download_media(self, tweets_data: list, username: str):
        """下载推文中的图片和视频"""
        user_media_dir = self.media_dir / username
        user_media_dir.mkdir(exist_ok=True)

        downloaded = 0
        async with aiohttp.ClientSession() as session:
            for tweet in tweets_data:
                if not tweet.get("media"):
                    continue

                for i, media in enumerate(tweet["media"]):
                    url = media.get("url")
                    if not url:
                        continue

                    # 处理 Twitter 图片 URL，获取最高质量
                    if "photo" in media.get("type", ""):
                        url = re.sub(r"&name=\w+", "", url)
                        url = f"{url}?format=jpg&name=4096x4096"

                    ext = "mp4" if "video" in media.get("type", "") else "jpg"
                    filename = f"{tweet['id']}_{i+1}.{ext}"
                    filepath = user_media_dir / filename

                    if filepath.exists():
                        continue

                    try:
                        async with session.get(url) as resp:
                            if resp.status == 200:
                                async with aiofiles.open(filepath, "wb") as f:
                                    await f.write(await resp.read())
                                downloaded += 1
                                media["local_path"] = str(filepath)
                    except Exception as e:
                        print(f"  [-] 下载失败 {filename}: {e}")

        print(f"[+] @{username} 媒体下载完成，共 {downloaded} 个文件")
        return downloaded

    def save_tweets(self, tweets_data: list, username: str):
        """保存推文数据"""
        save_format = self.config.get("save_format", "json")

        if save_format == "json":
            filepath = self.output_dir / f"{username}_tweets.json"
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(tweets_data, f, ensure_ascii=False, indent=2)
            print(f"[+] 数据已保存到: {filepath}")

        elif save_format == "csv":
            filepath = self.output_dir / f"{username}_tweets.csv"
            import csv

            with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
                if tweets_data:
                    fieldnames = [
                        "id", "url", "text", "created_at", "user",
                        "retweet_count", "favorite_count", "reply_count",
                        "view_count", "lang"
                    ]
                    writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(tweets_data)
            print(f"[+] 数据已保存到: {filepath}")

    async def run(self):
        """主运行流程"""
        print("=" * 50)
        print("  推特批量帖子获取工具")
        print("=" * 50)

        # 检查配置
        if not self._check_config():
            return

        # 登录
        if not await self.login():
            print("[-] 登录失败，退出")
            return

        target_users = self.config.get("target_users", [])
        max_tweets = self.config.get("max_tweets", 100)
        download_media_flag = self.config.get("download_media", True)

        for username in target_users:
            # 获取推文
            tweets_data = await self.get_user_tweets(username, max_tweets)

            if not tweets_data:
                print(f"[-] @{username} 没有获取到推文")
                continue

            # 下载媒体文件
            if download_media_flag:
                await self.download_media(tweets_data, username)

            # 保存数据
            self.save_tweets(tweets_data, username)

        print("\n[+] 全部完成！")


async def main():
    scraper = TwitterScraper()
    await scraper.run()


if __name__ == "__main__":
    asyncio.run(main())