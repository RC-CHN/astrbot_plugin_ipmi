import json
import asyncio
import os
import platform
import stat
import time
from pathlib import Path
from typing import Dict, Any, List

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# pyghmi is used as a fallback or for simple commands
import pyghmi.ipmi.command as ipmi_command


@register("astrbot_plugin_ipmi", "YourName", "使用内置或系统IPMI工具查询服务器信息", "1.5.0")
class IpmiPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.servers: List[Dict[str, Any]] = []
        self.sensor_groups: Dict[str, Dict[str, List[str]]] = {}
        logger.info("--- IPMI 插件初始化开始 ---")
        self.ipmitool_path = self._initialize_ipmitool()

        server_configs = self.config.get("servers", [])
        if not server_configs:
            logger.warning("IPMI 插件：未在配置中找到 'servers' 列表。")
            return

        for i, server_json in enumerate(server_configs):
            try:
                if not isinstance(server_json, str):
                    logger.error(f"IPMI 插件：第 {i+1} 个服务器配置不是字符串，已跳过。")
                    continue
                server_info = json.loads(server_json)
                if not isinstance(server_info, dict) or not all(k in server_info for k in ["name", "host", "username", "password"]):
                    logger.error(f"IPMI 插件：第 {i+1} 个服务器配置JSON格式错误或缺少键。")
                    continue

                if "sensor_groups" in server_info and isinstance(server_info["sensor_groups"], dict):
                    self.sensor_groups[server_info["name"]] = server_info["sensor_groups"]

                self.servers.append(server_info)
            except json.JSONDecodeError:
                logger.error(f"IPMI 插件：解析第 {i+1} 个服务器配置时出错: {server_json}")

        if self.servers:
            logger.info(f"IPMI 插件：成功加载 {len(self.servers)} 个服务器配置。")
            if self.sensor_groups:
                logger.info(f"已配置传感器组的服务器: {list(self.sensor_groups.keys())}")
        logger.info("--- IPMI 插件初始化完成 ---")

    def _initialize_ipmitool(self) -> str | None:
        plugin_dir = Path(__file__).parent
        system = platform.system()
        logger.debug(f"检测到当前操作系统: {system}")

        tool_name = "ipmitool"
        if system == "Windows":
            tool_name += ".exe"
            path_parts = ("IPMI_CLI", "win", tool_name)
        elif system == "Linux":
            path_parts = ("IPMI_CLI", "linux", tool_name)
        else:
            logger.warning(f"不支持的操作系统: {system}。ipmitool 功能将不可用。")
            return None

        tool_path = plugin_dir.joinpath(*path_parts)
        logger.debug(f"尝试定位 ipmitool: {tool_path}")

        if not tool_path.exists():
            logger.warning(f"未找到内置的 ipmitool: {tool_path}。sensor 相关功能将不可用。")
            return None

        if system == "Linux":
            try:
                current_permissions = tool_path.stat().st_mode
                if not (current_permissions & stat.S_IXUSR):
                    logger.info(f"'{tool_path}' 没有执行权限，尝试添加...")
                    os.chmod(tool_path, current_permissions | stat.S_IXUSR)
                    logger.info("权限添加成功。")
            except Exception as e:
                logger.error(f"为 ipmitool 添加执行权限时失败: {e}", exc_info=True)
                return None
        
        logger.info(f"IPMI 插件将使用内置的 ipmitool: {tool_path}")
        return str(tool_path)

    # --- Command Handlers Start ---
    
    def _find_target_server(self, target_name: str) -> Dict[str, Any] | None:
        """Helper to find a server configuration by its name."""
        return next((s for s in self.servers if s["name"] == target_name), None)

    @filter.command_group("ipmi")
    def ipmi(self, event: AstrMessageEvent):
        """Base command for IPMI, provides help text."""
        server_names = ", ".join([s['name'] for s in self.servers]) if self.servers else "无"
        usage = (
            f"IPMI 功能需要指定操作和服务器。\n可用服务器: {server_names}\n\n"
            f"用法: /ipmi <操作> <服务器别名> [详情]\n\n"
            f"示例:\n"
            f"  /ipmi power R4900G3\n"
            f"  /ipmi group R4900G3 temps\n"
            f"  /ipmi sel list R4900G3\n\n"
            f"可用操作: power, sensors, sensor, group, sel, fru, chassis"
        )
        yield event.plain_result(usage)

    @ipmi.command("power")
    async def ipmi_power(self, event: AstrMessageEvent, target_name: str):
        target_server = self._find_target_server(target_name)
        if not target_server:
            yield event.plain_result(f"未找到名为 '{target_name}' 的服务器。")
            return
        yield event.plain_result(f"正在对 '{target_name}' 查询电源状态...")
        result = await self.run_pyghmi_command(target_server, "power")
        yield event.plain_result(result)

    @ipmi.command("sensors")
    async def ipmi_sensors(self, event: AstrMessageEvent, target_name: str):
        target_server = self._find_target_server(target_name)
        if not target_server:
            yield event.plain_result(f"未找到名为 '{target_name}' 的服务器。")
            return
        yield event.plain_result(f"正在对 '{target_name}' 查询所有传感器...")
        result = await self.run_ipmitool_cli_streaming(target_server, ["sensor", "list"])
        yield event.plain_result(result)

    @ipmi.command("sensor")
    async def ipmi_sensor(self, event: AstrMessageEvent, target_name: str, sensor_name: str):
        target_server = self._find_target_server(target_name)
        if not target_server:
            yield event.plain_result(f"未找到名为 '{target_name}' 的服务器。")
            return
        yield event.plain_result(f"正在对 '{target_name}' 查询传感器 '{sensor_name}'...")
        result = await self.run_ipmitool_cli_streaming(target_server, ["sensor", "get", sensor_name])
        yield event.plain_result(result)
        
    @ipmi.command("group")
    async def ipmi_group(self, event: AstrMessageEvent, target_name: str, group_name: str):
        target_server = self._find_target_server(target_name)
        if not target_server:
            yield event.plain_result(f"未找到名为 '{target_name}' 的服务器。")
            return
        
        groups = self.sensor_groups.get(target_name, {})
        if not group_name or group_name not in groups:
             result = f"请提供有效的传感器组名称。\n可用组: {', '.join(groups.keys()) if groups else '无'}"
             yield event.plain_result(result)
             return

        yield event.plain_result(f"正在对 '{target_name}' 查询传感器组 '{group_name}'...")
        result = await self._handle_sensor_group(target_server, group_name)
        yield event.plain_result(result)

    @ipmi.command("fru")
    async def ipmi_fru(self, event: AstrMessageEvent, target_name: str):
        target_server = self._find_target_server(target_name)
        if not target_server:
            yield event.plain_result(f"未找到名为 '{target_name}' 的服务器。")
            return
        yield event.plain_result(f"正在对 '{target_name}' 查询 FRU 信息...")
        result = await self.run_ipmitool_cli_streaming(target_server, ["fru"])
        yield event.plain_result(result)
        
    @ipmi.command("sel")
    async def ipmi_sel(self, event: AstrMessageEvent, subcommand: str, target_name: str):
        if subcommand.lower() != "list":
            yield event.plain_result(f"未知 'sel' 子命令: '{subcommand}'. 可用: list")
            return
            
        target_server = self._find_target_server(target_name)
        if not target_server:
            yield event.plain_result(f"未找到名为 '{target_name}' 的服务器。")
            return
        yield event.plain_result(f"正在对 '{target_name}' 查询 SEL 日志...")
        result = await self.run_ipmitool_cli_streaming(target_server, ["sel", "list"])
        yield event.plain_result(result)

    @ipmi.command("chassis")
    async def ipmi_chassis(self, event: AstrMessageEvent, subcommand: str, target_name: str):
        if subcommand.lower() != "status":
            yield event.plain_result(f"未知 'chassis' 子命令: '{subcommand}'. 可用: status")
            return

        target_server = self._find_target_server(target_name)
        if not target_server:
            yield event.plain_result(f"未找到名为 '{target_name}' 的服务器。")
            return
        yield event.plain_result(f"正在对 '{target_name}' 查询机箱状态...")
        result = await self.run_ipmitool_cli_streaming(target_server, ["chassis", "status"])
        yield event.plain_result(result)
        
    # --- Helper Methods ---

    async def _handle_sensor_group(self, server_info: dict, group_name: str) -> str:
        server_name = server_info["name"]
        groups = self.sensor_groups.get(server_name, {})
        sensor_list = groups.get(group_name)

        if not sensor_list:
            return f"在服务器 '{server_name}' 中未找到名为 '{group_name}' 的传感器组。"

        tasks = [self.run_ipmitool_cli_streaming(server_info, ["sensor", "get", sensor]) for sensor in sensor_list]
        results = await asyncio.gather(*tasks)
        return f"传感器组 '{group_name}' 查询结果:\n" + "\n".join(results)

    async def run_ipmitool_cli_streaming(self, server_info: dict, command_parts: list) -> str:
        if not self.ipmitool_path:
            return "错误: 未找到 ipmitool, 无法执行此操作。"
            
        base_cmd = [
            self.ipmitool_path, "-I", "lanplus",
            "-H", server_info['host'], "-U", server_info['username'],
            "-P", server_info['password']
        ]
        full_cmd_list = base_cmd + command_parts
        tool_dir = str(Path(self.ipmitool_path).parent)
        
        start_time = time.monotonic()
        logger.info(f"[{start_time:.2f}] 即将执行: {' '.join(full_cmd_list)}")

        try:
            process = await asyncio.create_subprocess_exec(
                *full_cmd_list,
                cwd=tool_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            end_time = time.monotonic()
            logger.debug(f"[{end_time:.2f}] 子进程退出，返回码: {process.returncode}. 总耗时: {end_time - start_time:.2f}s.")

            if process.returncode == 0:
                full_output = stdout.decode('utf-8', errors='ignore').strip()
                return f"执行 'ipmitool {' '.join(command_parts)}' 成功:\n```\n{full_output}\n```"
            else:
                full_error = stderr.decode('utf-8', errors='ignore').strip()
                return f"执行 'ipmitool {' '.join(command_parts)}' 失败 (Code: {process.returncode}):\n```\n{full_error}\n```"
        except FileNotFoundError:
            logger.error(f"命令未找到: {self.ipmitool_path}")
            return "错误: ipmitool 可执行文件未找到。"
        except Exception as e:
            logger.error(f"执行ipmitool时出错: {e}", exc_info=True)
            return f"执行ipmitool时发生未知错误: {e}"

    async def run_pyghmi_command(self, server_info: dict, operation: str) -> str:
        logger.debug(f"准备在线程中执行 pyghmi 操作: {operation}")
        
        def ipmi_sync_call():
            try:
                cmd = ipmi_command.Command(
                    bmc=server_info['host'], userid=server_info['username'], password=server_info['password']
                )
                if operation.lower() == 'power':
                    state = cmd.get_power().get('powerstate', '未知')
                    return f"服务器 '{server_info['name']}' 的电源状态是: {state}"
                return "内部错误: pyghmi 不应处理此操作。"
            except Exception as e:
                logger.error(f"pyghmi 同步调用失败: {e}", exc_info=True)
                return f"通过 pyghmi 获取信息失败: {e}"

        loop = asyncio.get_running_loop()
        result_str = await loop.run_in_executor(None, ipmi_sync_call)
        logger.debug(f"pyghmi 线程执行完毕，返回: {result_str}")
        return result_str

    async def terminate(self):
        """插件销毁时的清理工作"""
        logger.info("IPMI 插件已卸载。")
