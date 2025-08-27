import json
import asyncio
import os
import platform
import stat
import time
from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# pyghmi is used as a fallback or for simple commands
import pyghmi.ipmi.command as ipmi_command


@register("ipmi_query", "YourName", "使用内置或系统IPMI工具查询服务器信息", "1.4.0-final-debug")
class IpmiPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.servers = []
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
                self.servers.append(server_info)
            except json.JSONDecodeError:
                logger.error(f"IPMI 插件：解析第 {i+1} 个服务器配置时出错: {server_json}")
        
        if self.servers:
            logger.info(f"IPMI 插件：成功加载 {len(self.servers)} 个服务器配置。")
        logger.info("--- IPMI 插件初始化完成 ---")

    def _initialize_ipmitool(self) -> str | None:
        """Determines the path to the bundled ipmitool and sets permissions."""
        plugin_dir = Path(__file__).parent
        system = platform.system()
        logger.debug(f"检测到当前操作系统: {system}")
        
        if system == "Linux":
            tool_path = plugin_dir / "IPMI_CLI" / "linux" / "ipmitool"
            logger.debug(f"尝试定位 Linux ipmitool: {tool_path}")
            if tool_path.exists():
                try:
                    current_permissions = tool_path.stat().st_mode
                    if not (current_permissions & stat.S_IXUSR):
                        logger.info(f"'{tool_path}' 没有执行权限，尝试添加...")
                        os.chmod(tool_path, current_permissions | stat.S_IXUSR)
                        logger.info(f"权限添加成功。")
                    logger.info(f"IPMI 插件将使用内置的 Linux ipmitool: {tool_path}")
                    return str(tool_path)
                except Exception as e:
                    logger.error(f"为 ipmitool 添加执行权限时失败: {e}", exc_info=True)
                    return None
        elif system == "Windows":
            tool_path = plugin_dir / "IPMI_CLI" / "win" / "ipmitool.exe"
            logger.debug(f"尝试定位 Windows ipmitool: {tool_path}")
            if tool_path.exists():
                logger.info(f"IPMI 插件将使用内置的 Windows ipmitool: {tool_path}")
                return str(tool_path)

        logger.warning("未找到内置的 ipmitool, sensor 相关功能将不可用。")
        return None

    @filter.command("ipmi")
    async def ipmi_query(self, event: AstrMessageEvent, target_name: str = "", operation: str = "", detail: str = ""):
        logger.debug(f"IPMI 指令触发: target='{target_name}', operation='{operation}', detail='{detail}'")
        
        if not self.servers:
            yield event.plain_result("IPMI 插件尚未配置任何服务器。")
            return

        server_names = ", ".join([s['name'] for s in self.servers])
        if not target_name:
            yield event.plain_result(f"请输入服务器别名。\n可用: {server_names}\n\n用法:\n/ipmi <别名> <power|sensors|sensor> [详情]")
            return

        target_server = next((s for s in self.servers if s["name"] == target_name), None)
        if not target_server:
            yield event.plain_result(f"未找到名为 '{target_name}' 的服务器。\n可用: {server_names}")
            return
        
        op = operation.lower()
        full_op_str = f"{op} {detail}".strip()
        yield event.plain_result(f"正在对 '{target_name}' ({target_server['host']}) 执行 '{full_op_str}'...")
        
        try:
            result = ""
            # --- Main logic dispatch ---
            if op in ["sensors", "sensor"]:
                logger.debug(f"操作 '{op}' 属于 sensor 类型。")
                if self.ipmitool_path:
                    logger.debug(f"检测到 ipmitool 路径: {self.ipmitool_path}, 将使用 CLI。")
                    cli_command_parts = []
                    if op == "sensors":
                        cli_command_parts = ["sensor", "list"]
                    elif op == "sensor":
                        if not detail:
                            yield event.plain_result("请提供要查询的传感器名称, 例如: /ipmi my-server sensor CPU1_Temp")
                            return
                        # Pass sensor name as a separate argument to handle spaces correctly
                        cli_command_parts = ["sensor", "get", detail]
                    
                    result = await self.run_ipmitool_cli_streaming(target_server, cli_command_parts)
                else:
                    logger.error("无法执行 sensor 操作，因为 ipmitool_path 未设置。")
                    result = "错误: 未找到内置的 ipmitool, 无法执行 sensor 操作。"
            
            elif op == "power":
                logger.debug(f"操作 '{op}' 属于 power 类型，将使用 pyghmi。")
                result = await self.run_pyghmi_command(target_server, op)
            else:
                logger.warning(f"收到不支持的操作: '{op}'")
                result = f"不支持的操作: '{op}'。可用操作: power, sensors, sensor。"

            yield event.plain_result(result)

        except Exception as e:
            logger.error(f"IPMI 指令处理时发生顶层异常: {e}", exc_info=True)
            yield event.plain_result(f"执行操作失败，请检查日志。")

    async def run_ipmitool_cli_streaming(self, server_info: dict, command_parts: list) -> str:
        """Executes the bundled ipmitool and reads its output line by line."""
        base_cmd = [
            self.ipmitool_path, "-I", "lanplus",
            "-H", server_info['host'], "-U", server_info['username'],
            "-P", server_info['password']
        ]
        full_cmd_list = base_cmd + command_parts
        
        # Set the CWD to the directory containing the executable, so it can find its DLLs.
        tool_dir = str(Path(self.ipmitool_path).parent)
        
        start_time = time.monotonic()
        logger.info(f"[{start_time:.2f}] 即将执行: {full_cmd_list} in CWD: {tool_dir}")

        process = await asyncio.create_subprocess_exec(
            *full_cmd_list,
            cwd=tool_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        process_creation_time = time.monotonic()
        logger.debug(f"[{process_creation_time:.2f}] 子进程已创建，耗时: {process_creation_time - start_time:.2f}s. 开始流式读取...")
        
        output_lines = []
        error_lines = []
        first_line_time = None

        async def read_stream(stream, line_list, log_prefix):
            nonlocal first_line_time
            while True:
                line = await stream.readline()
                if first_line_time is None and line:
                    first_line_time = time.monotonic()
                    logger.debug(f"[{first_line_time:.2f}] 收到第一行输出，耗时: {first_line_time - process_creation_time:.2f}s.")
                
                if not line:
                    break
                decoded_line = line.decode('utf-8', errors='ignore').strip()
                logger.debug(f"{log_prefix}: {decoded_line}")
                line_list.append(decoded_line)

        await asyncio.gather(
            read_stream(process.stdout, output_lines, "STDOUT"),
            read_stream(process.stderr, error_lines, "STDERR")
        )
        
        await process.wait()
        end_time = time.monotonic()
        logger.debug(f"[{end_time:.2f}] 子进程已退出，返回码: {process.returncode}. 总耗时: {end_time - start_time:.2f}s.")

        if process.returncode == 0:
            full_output = "\n".join(output_lines)
            return f"执行 'ipmitool {' '.join(command_parts)}' 成功:\n```\n{full_output}\n```"
        else:
            full_error = "\n".join(error_lines)
            return f"执行 'ipmitool {' '.join(command_parts)}' 失败 (Code: {process.returncode}):\n```\n{full_error}\n```"

    async def run_pyghmi_command(self, server_info: dict, operation: str) -> str:
        """Runs pyghmi commands in a thread. ONLY for non-sensor commands."""
        
        logger.debug(f"准备在线程中执行 pyghmi 操作: {operation}")
        def ipmi_sync_call():
            logger.debug("进入 pyghmi 同步执行函数。")
            cmd = ipmi_command.Command(
                bmc=server_info['host'], userid=server_info['username'], password=server_info['password']
            )
            op = operation.lower()
            if op == 'power':
                state = cmd.get_power().get('powerstate', '未知')
                logger.debug(f"pyghmi 获取到电源状态: {state}")
                return f"服务器 '{server_info['name']}' 的电源状态是: {state}"
            return "内部错误: pyghmi 不应处理此操作。"

        loop = asyncio.get_running_loop()
        result_str = await loop.run_in_executor(None, ipmi_sync_call)
        logger.debug(f"pyghmi 线程执行完毕，返回: {result_str}")
        return result_str

    async def terminate(self):
        """插件销毁时的清理工作"""
        logger.info("IPMI 插件已卸载。")
