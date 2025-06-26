#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import logging
import socket
import sys
from ipaddress import IPv4Address
from typing import Annotated

import dns.exception
import dns.query
import dns.rcode
import dns.tsigkeyring
import dns.update
import psutil
from pydantic import Field, IPvAnyAddress
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)


# 定义TSIG密钥模型
class TSIGKey(BaseSettings):
    name: Annotated[str, Field(description="TSIG密钥名称")] = ""
    algorithm: Annotated[
        str,
        Field(
            description="TSIG算法",
            pattern=r"^(hmac-sha256|hmac-sha384|hmac-sha512)$",
        ),
    ] = "hmac-sha256"
    secret: Annotated[str, Field(description="TSIG密钥(Base64编码)")] = ""
    model_config = SettingsConfigDict()


# 定义配置模型
class Config(BaseSettings):
    tsig: list[TSIGKey] = []
    name_server: Annotated[IPvAnyAddress | None, Field(description="DNS服务器地址")] = (
        None
    )
    zone: Annotated[str, Field(description="DNS区域", pattern=r"^.+\.?$")] = ""
    hostname: Annotated[str, Field(description="更新记录的主机名")] = ""
    ttl: Annotated[int, Field(description="DNS记录TTL")] = 3600
    timeout: Annotated[int, Field(description="连接超时(秒)")] = 5
    protocol: Annotated[
        str,
        Field(
            description="通讯协议",
            pattern=r"^(tcp|udp)$",
        ),
    ] = "udp"
    interface: Annotated[str | None, Field(description="网络接口名称")] = None
    log_level: Annotated[
        str,
        Field(
            description="日志级别",
            pattern=r"^(info|debug)$",
        ),
    ] = "info"

    model_config = SettingsConfigDict(
        toml_file="config.toml",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return init_settings, TomlConfigSettingsSource(settings_cls=settings_cls)


# 配置日志格式
def setup_logging(log_level):
    level_map = {
        "info": logging.INFO,
        "debug": logging.DEBUG,
    }
    level = level_map.get(log_level.lower(), logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler("ddns.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("ddns")


# 获取接口IP地址
def get_interface_ip(interface_name: str | None) -> IPv4Address | None:
    special_interfaces = ["wlan", "以太网", "ethernet"]  # 需要检查的接口名称列表
    try:
        # 获取所有网络接口
        net_if_addrs = psutil.net_if_addrs()

        if interface_name is None:
            # 如果没有指定接口名称，则返回第一个接口为某个列表中（比如“WLAN”，“以太网”）的IPV4地址
            for name, addrs in net_if_addrs.items():
                if name.lower() in special_interfaces:
                    netif = addrs
                    break
        else:
            if interface_name not in net_if_addrs:
                raise ValueError(f"找不到接口: {interface_name}")
            netif = net_if_addrs[interface_name]

        # 获取IPv4地址
        for addr in netif:
            if addr.family == socket.AF_INET:  # IPv4
                ip = IPv4Address(addr.address)
                # 检查不是回环地址、链路本地地址和保留地址
                if not ip.is_loopback and not ip.is_link_local and not ip.is_reserved:
                    return ip
    except Exception as e:
        logger.error(f"获取接口IP地址失败: {e}")

    return None


# 解析命令行参数
def parse_args():
    parser = argparse.ArgumentParser(description="DDNS客户端")
    parser.add_argument("--name-server", help="DNS服务器地址")
    parser.add_argument("--zone", help="DNS区域")
    parser.add_argument("--hostname", help="主机名")
    parser.add_argument("--ttl", type=int, help="DNS记录TTL", default=300)
    parser.add_argument("--timeout", type=int, help="连接超时(秒)", default=5)
    parser.add_argument(
        "--protocol", choices=["tcp", "udp"], help="通讯协议", default="udp"
    )
    parser.add_argument("--interface", help="网络接口名称")
    parser.add_argument(
        "--log-level",
        choices=["info", "debug"],
        help="日志级别",
        default="info",
    )
    parser.add_argument("--tsig-name", help="TSIG密钥名称")
    parser.add_argument(
        "--tsig-algorithm",
        choices=["hmac-sha256", "hmac-sha384", "hmac-sha512"],
        help="TSIG算法",
        default="hmac-sha256",
    )
    parser.add_argument("--tsig-secret", help="TSIG密钥(Base64编码)")

    return parser.parse_args()


# 主函数
def main():
    # 检查必要参数
    if config.name_server is None or config.name_server == "":
        logger.error("未指定DNS服务器地址")
        sys.exit(1)
    if config.zone == "":
        logger.error("未指定DNS区域")
        sys.exit(1)
    if config.hostname == "":
        logger.error("未指定主机名")
        sys.exit(1)
    if config.tsig == []:
        logger.error("未指定TSIG密钥信息")
        sys.exit(1)

    # 准备TSIG keyring
    keyring = dns.tsigkeyring.from_text(
        {key.name: (key.algorithm, key.secret) for key in config.tsig},
    )

    # 获取IP地址
    ip_address = get_interface_ip(config.interface)
    if ip_address is None:
        logger.error(f"无法从接口 {config.interface} 获取IP地址")
        sys.exit(1)

    logger.info(f"正在更新DNS记录: {config.hostname}.{config.zone} -> {ip_address}")

    # 构造Update消息
    update = dns.update.Update(
        zone=config.zone,
        keyring=keyring,
    )

    update.replace(config.hostname, config.ttl, "A", ip_address.compressed)

    # 发送更新请求
    try:
        if config.protocol in dir(dns.query):
            query_executor = getattr(dns.query, config.protocol)
            response = query_executor(
                update,
                config.name_server.compressed,
                timeout=config.timeout,
            )
        else:
            raise ValueError(f"不支持的协议: {config.protocol}")

        if response.rcode() == dns.rcode.NOERROR:
            logger.info(f"DNS更新成功: id({response.id})")
        elif response.rcode() == dns.rcode.NXDOMAIN:
            logger.error(
                f"DNS更新失败: 域名不存在，hostname: {config.hostname}", exc_info=True
            )
        elif response.rcode() == dns.rcode.SERVFAIL:
            logger.error(
                f"DNS更新失败: 服务失败，hostname: {config.hostname}", exc_info=True
            )
        elif response.rcode() == dns.rcode.NOTAUTH:
            logger.error(
                f"DNS更新失败: 未授权，hostname: {config.hostname}", exc_info=True
            )
        elif response.rcode() == dns.rcode.NOTZONE:
            logger.error(
                f"DNS更新失败: 非区域，hostname: {config.hostname}", exc_info=True
            )
        elif response.rcode() == dns.rcode.REFUSED:
            logger.error(
                f"DNS更新失败: 请求被拒绝，hostname: {config.hostname}", exc_info=True
            )
        else:
            logger.error(
                f"DNS更新失败: 更新过程出现未知错误 {response.rcode()}\n{response}",
                exc_info=True,
            )
        logger.debug(
            f"更新信息： {update}, DNS服务器： {config.name_server} DNS响应: {response.rcode()}\n{response}"
        )
    except dns.exception.DNSException as e:
        logger.error(f"DNS更新失败: {e}", exc_info=True)
        logger.debug(
            f"更新信息: {update}, Hostname: {config.hostname}, DNS服务器: {config.name_server}"
        )


if __name__ == "__main__":
    args = parse_args()

    config = Config()

    # 命令行参数覆盖配置文件
    if args.name_server is not None:
        config.name_server = args.name_server
    if args.zone is not None:
        config.zone = args.zone
    if args.hostname is not None:
        config.hostname = args.hostname
    if args.ttl is not None:
        config.ttl = args.ttl
    if args.timeout is not None:
        config.timeout = args.timeout
    if args.protocol is not None:
        config.protocol = args.protocol
    if args.interface is not None:
        config.interface = args.interface
    if args.log_level is not None:
        config.log_level = args.log_level
    if args.tsig_name is not None and args.tsig_secret is not None:
        config.tsig.append(
            TSIGKey(
                name=args.tsig_name,
                algorithm=args.tsig_algorithm,
                secret=args.tsig_secret,
            )
        )

    # 确保zone以点号结尾
    config.zone = f"{config.zone}." if not config.zone.endswith(".") else config.zone

    # 设置日志级别
    logger = setup_logging(config.log_level)
    main()
