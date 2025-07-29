# -*- coding:utf-8 -*-
import os
import requests
import hashlib
import time
import copy
import logging
import random
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
from config import Config

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

ENV = os.environ

s = requests.Session()
adapter = requests.adapters.HTTPAdapter(
    pool_connections=Config.HTTP_SETTINGS["POOL_CONNECTIONS"],  # 连接池的连接数
    pool_maxsize=Config.HTTP_SETTINGS["POOL_MAXSIZE"],  # 连接池的最大数量
    max_retries=Config.HTTP_SETTINGS["RETRY_TIMES"],  # 最大重试次数
    pool_block=False,  # 连接池满时不阻塞
)
s.mount("http://", adapter)
s.mount("https://", adapter)


def get_tbs(bduss):
    logger.info("获取tbs开始")
    headers = copy.copy(Config.HEADERS)
    headers.update({"Cookie": f"BDUSS={bduss}"})
    try:
        tbs = s.get(
            url=Config.API_URLS["TBS_URL"],
            headers=headers,
            timeout=Config.HTTP_SETTINGS["TIMEOUT"],
        ).json()["tbs"]
    except Exception as e:
        logger.error("获取tbs出错: %s", e)
        logger.info("重新获取tbs开始")
        tbs = s.get(
            url=Config.API_URLS["TBS_URL"],
            headers=headers,
            timeout=Config.HTTP_SETTINGS["TIMEOUT"],
        ).json()["tbs"]
    logger.info("获取tbs结束")
    return tbs


def get_favorite(bduss):
    logger.info("获取关注的贴吧开始")
    all_bars = []
    page = 1

    while True:
        data = {
            "BDUSS": bduss,
            "_client_type": "2",
            "_client_id": "wappc_1534235498291_488",
            "_client_version": "9.7.8.0",
            "_phone_imei": "000000000000000",
            "from": "1008621y",
            "page_no": str(page),
            "page_size": "200",
            "model": "MI+5",
            "net_type": "1",
            "timestamp": str(int(time.time())),
            "vcode_tag": "11",
        }

        try:
            res = s.post(
                url=Config.API_URLS["LIKE_URL"],
                data=encodeData(data),
                timeout=Config.HTTP_SETTINGS["TIMEOUT"],
            ).json()

            if not res.get("forum_list"):
                break

            for forum_type in ["non-gconforum", "gconforum"]:
                if forum_type in res["forum_list"]:
                    items = res["forum_list"][forum_type]
                    if isinstance(items, list):
                        all_bars.extend(items)
                    else:
                        all_bars.append(items)

            if res.get("has_more") != "1":
                break

            page += 1

        except Exception as e:
            logger.error(f"获取第{page}页贴吧列表失败: {e}")
            break

    logger.info(f"共获取到{len(all_bars)}个贴吧")
    return all_bars


def encodeData(data):
    s = ""
    keys = data.keys()
    for i in sorted(keys):
        s += i + "=" + str(data[i])
    sign = hashlib.md5((s + "tiebaclient!!!").encode("utf-8")).hexdigest().upper()
    data.update({"sign": str(sign)})
    return data


def client_sign(bduss, tbs, fid, kw):
    logger.info("开始签到贴吧：" + kw)
    data = copy.copy(Config.SIGN_DATA)
    data.update(
        {
            "BDUSS": bduss,
            "fid": fid,
            "kw": kw,
            "tbs": tbs,
            "timestamp": str(int(time.time())),
        }
    )
    data = encodeData(data)
    res = s.post(
        url=Config.API_URLS["SIGN_URL"],
        data=data,
        timeout=Config.HTTP_SETTINGS["TIMEOUT"],
    ).json()
    return res


def sign_one_bar(args):
    bduss, tbs, bar = args
    try:
        time.sleep(
            random.randint(
                Config.THREAD_SETTINGS["MIN_DELAY"], Config.THREAD_SETTINGS["MAX_DELAY"]
            )
        )
        res = client_sign(bduss, tbs, bar["id"], bar["name"])

        error_code = res.get("error_code", "unknown")
        status = Config.ERROR_CODES.get(str(error_code), f"未知错误: {error_code}")

        if error_code in Config.SUCCESS_CODES:
            logger.info(f'贴吧：{bar["name"]} 签到状态：{status}')
        elif error_code in Config.CRITICAL_ERRORS:
            logger.warning(f'贴吧：{bar["name"]} 签到状态：{status}')
        else:
            logger.error(f'贴吧：{bar["name"]} 签到状态：{status}')

        return {
            "name": bar["name"],
            "bar": bar,
            "status": status,
            "error_code": error_code,
            "is_success": error_code in Config.SUCCESS_CODES,
        }
    except Exception as e:
        logger.error(f'贴吧：{bar["name"]} 签到异常：{str(e)}')
        return {
            "name": bar["name"],
            "bar": bar,
            "status": "签到异常",
            "error": str(e),
            "is_success": False,
        }


def send_email(results):
    """发送签到结果邮件"""
    # 检查必要的环境变量
    required_env = [
        "SMTP_HOST",  # 必需：邮件服务器地址
        "SMTP_PORT",  # 必需：服务器端口
        "SMTP_USER",  # 必需：邮箱账号
        "SMTP_PASS",  # 必需：授权码/密码
        "SENDER",  # 必需：发件人地址
        "RECEIVER",  # 必需：收件人地址
    ]
    if not all(key in ENV for key in required_env):
        logger.warning("邮件环境变量不完整，跳过发送邮件")
        return

    # 统计签到结果
    total = len(results)
    success = len([r for r in results if r.get("is_success")])

    # 构建邮件内容
    html_content = f"""
    <h3>贴吧签到统计</h3>
    <p>总共签到：{total} 个贴吧</p>
    <p>成功签到：{success} 个贴吧</p>
    <p>失败签到：{total - success} 个贴吧</p>
    <h3>详细签到情况：</h3>
    <table border="1">
        <tr><th>贴吧名称</th><th>签到状态</th></tr>
        {''.join(f'<tr><td>{r["name"]}</td><td>{r["status"]}</td></tr>' for r in results)}
    </table>
    """

    try:
        msg = MIMEMultipart()
        msg["From"] = Header(ENV["SENDER"])
        msg["To"] = Header(ENV["RECEIVER"])
        # 获取北京时间
        beijing_time = time.localtime(time.time() + 8 * 3600)  # UTC+8
        date_str = time.strftime("%Y-%m-%d", beijing_time)

        msg["Subject"] = Header(f"贴吧签到结果通知 - {date_str}")

        msg.attach(MIMEText(html_content, "html", "utf-8"))

        # 添加详细的连接日志
        logger.info(f"尝试连接 SMTP 服务器: {ENV['SMTP_HOST']}:{ENV['SMTP_PORT']}")

        try:
            smtp = smtplib.SMTP_SSL(ENV["SMTP_HOST"], int(ENV["SMTP_PORT"]))
        except Exception as e:
            logger.error(f"SMTP 服务器连接失败: {e}")
            # 尝试不使用 SSL
            logger.info("尝试使用非 SSL 连接")
            smtp = smtplib.SMTP(ENV["SMTP_HOST"], int(ENV["SMTP_PORT"]))
            smtp.starttls()  # 启用 TLS 加密

        logger.info("SMTP 服务器连接成功，尝试登录")
        smtp.login(ENV["SMTP_USER"], ENV["SMTP_PASS"])
        logger.info("SMTP 登录成功，开始发送邮件")

        smtp.send_message(msg)
        smtp.quit()
        logger.info("签到结果邮件发送成功")
    except smtplib.SMTPAuthenticationError as e:
        logger.error(f"SMTP 认证失败，请检查用户名和密码: {e}")
    except smtplib.SMTPException as e:
        logger.error(f"SMTP 错误: {e}")
    except Exception as e:
        logger.error(f"发送邮件失败: {str(e)}")


def main():
    if "BDUSS" not in ENV:
        logger.error("未配置BDUSS")
        return

    b = ENV["BDUSS"].split("#")
    max_workers = min(Config.THREAD_SETTINGS["MAX_WORKERS"], os.cpu_count() * 5)

    for n, bduss in enumerate(b):
        logger.info(f"开始签到第{n+1}个用户")
        try:
            tbs = get_tbs(bduss)
            favorites = get_favorite(bduss)
        except Exception as e:
            logger.error(f"用户签到失败: {e}")
            continue

        rounds = 0
        to_sign = favorites[:]
        all_results = []

        while rounds < 1 and to_sign:  # 当 to_sign 为空时，循环会结束
            rounds += 1
            logger.info(f"开始第 {rounds} 轮签到，待签到贴吧数：{len(to_sign)}")
            results = []

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_bar = {
                    executor.submit(sign_one_bar, (bduss, tbs, bar)): bar
                    for bar in to_sign
                }
                # 并发执行签到
                for future in concurrent.futures.as_completed(future_to_bar):
                    result = future.result()
                    if result:
                        results.append(result)
                        if result.get("error_code") in Config.CRITICAL_ERRORS:
                            logger.warning(
                                f"检测到严重问题：{result['status']}，停止当前用户的签到"
                            )
                            executor._threads.clear()
                            break

            all_results.extend(results)

            # 过滤出未签到成功的贴吧
            to_sign = [
                result["bar"] for result in results if not result.get("is_success")
            ]

            if to_sign:
                logger.info(f"本轮签到完成，等待1分钟后进行下一轮签到")
                time.sleep(60)
            else:
                logger.info("所有贴吧签到成功，结束签到")
                break

        if all_results:
            send_email(all_results)


if __name__ == "__main__":
    main()
