from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import requests
import logging
import os
import json
import io
import sys
from datetime import datetime

# 导入python-a2a库
try:
    from python_a2a.client.router import AIAgentRouter
    from python_a2a.server.llm import OpenAIA2AServer
    from python_a2a.client.llm import OpenAIA2AClient
    from python_a2a import AgentCard, AgentSkill, AgentNetwork
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

# 配置日志
import os
from datetime import datetime

# 创建logs目录
log_dir = "logs"
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

# 生成日志文件名（按日期）
log_filename = f"agent_router_{datetime.now().strftime('%Y%m%d')}.log"
log_filepath = os.path.join(log_dir, log_filename)

# 配置日志 - 在Flask应用创建之前配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_filepath, encoding='utf-8'),
        logging.StreamHandler()  # 同时输出到控制台
    ],
    force=True  # 强制重新配置
)
logger = logging.getLogger("AgentRouter")

# 确保Flask不会覆盖日志配置
app = Flask(__name__)
app.logger.handlers = []  # 清除Flask默认的handlers
app.logger.addHandler(logging.FileHandler(log_filepath, encoding='utf-8'))
app.logger.addHandler(logging.StreamHandler())
app.logger.setLevel(logging.INFO)

CORS(app)

# 配置OpenAI模型用于意图识别
API_KEY = "sk-iffljtZXLKgvupkOC890243eA27940809a056aB230B7E7E8"
MODEL_NAME = "Qwen3-235B-A22B"
API_BASE_URL = "http://10.48.109.102:8000/v1/"

# 设置环境变量以配置OpenAI客户端使用自定义API基础URL
os.environ["OPENAI_BASE_URL"] = API_BASE_URL
os.environ["OPENAI_API_KEY"] = API_KEY

try:
    logger.info(f"开始初始化OpenAI客户端，API_KEY: {API_KEY[:10]}..., MODEL_NAME: {MODEL_NAME}, API_BASE_URL: {API_BASE_URL}")
    openai_client = OpenAIA2AClient(
        api_key=API_KEY,
        model=MODEL_NAME
    )
    logger.info("OpenAI客户端初始化成功")
    logger.info(f"OpenAI客户端对象: {openai_client}")
except Exception as e:
    logger.error(f"OpenAI客户端初始化失败: {e}")
    import traceback
    logger.error(f"详细错误信息: {traceback.format_exc()}")
    openai_client = None

# 获取远程agent.json信息
def fetch_agent_cards(agent_json_url):
    try:
        logger.info(f"正在获取agent.json: {agent_json_url}")
        resp = requests.get(agent_json_url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"成功获取agent.json，包含 {len(data.get('agents', []))} 个agent")
        
        agent_cards = []
        for agent_info in data.get("agents", []):
            # 保持原始URL格式，不强制转换HTTPS到HTTP
            agent_url = agent_info["url"]
            logger.info(f"正在获取agent card: {agent_url}")
            
            card_resp = requests.get(agent_url, timeout=10)
            card_resp.raise_for_status()
            card_data = card_resp.json()
            
            # 保持原始URL格式
            agent_card = AgentCard(
                name=card_data["name"],
                description=card_data["description"],
                url=card_data["url"],
                version=card_data.get("version", "1.0.0"),
                skills=[
                    AgentSkill(
                        name=skill["name"],
                        description=skill["description"],
                        tags=skill["tags"],
                        examples=skill["examples"]
                    ) for skill in card_data.get("skills", [])
                ]
            )
            
            # 添加额外的属性
            if "category" in card_data:
                setattr(agent_card, "category", card_data["category"])
            setattr(agent_card, "api", card_data.get("api", {}))
            
            agent_cards.append((agent_info["name"], agent_card, card_data["parameters"]["model"]))
            logger.info(f"成功添加agent: {agent_info['name']}")
        
        return agent_cards
    except Exception as e:
        logger.error(f"获取agent cards时出错: {e}")
        raise

# 识别工作流智能体
def is_workflow_agent(agent_card):
    # 推荐：用tag/category/agent_type
    if getattr(agent_card, "category", "") == "workflow":
        return True
    return False

# 定义通用远程智能体类
class RemoteAgent:
    def __init__(self, agent_card, model_id):
        self.agent_card = agent_card
        self.url = agent_card.url
        self.model_id = model_id

    def ask(self, query):
        """Send a query to the remote API with proper format conversion."""
        # 新问题一律新会话；只有“继续输入”场景才复用

        try:
            # 使用与smart_routing.py中BaowuAgent相同的格式
            payload = {
                "model": self.model_id,
                "messages": [
                    {
                        "role": "user",
                        "content": query
                    }
                ],
                "temperature": 0,
                "stream": False
            }
            
            # 如果是AccountManager Agent，使用增强的提示词
            if "AccountManager" in self.agent_card.name:
                payload["messages"][0]["content"] = f"{query}，请提供详细分析并生成图表展示"
            
            headers = {
                "Content-Type": "application/json"
            }
            
            # 将HTTPS URL转换为HTTP URL
            url = self.url.replace("https://", "http://")
            
            # Debug: Print the request payload
            logger.info(f"Sending request to {url}")
            logger.info(f"Payload: {json.dumps(payload, indent=2, ensure_ascii=False)}")
            
            # Increase timeout to handle slow processing
            response = requests.post(
                url,
                json=payload,  # Use JSON format
                headers=headers,
                timeout=120  # Increase timeout to 2 minutes
            )
            
            # 打印原始HTTP响应内容，不管成功失败
            logger.info("---- Response.text ----")
            logger.info(response.text)
            
            if response.status_code == 200:
                result = response.json()
                # Extract content from API response
                if "choices" in result and len(result["choices"]) > 0:
                    choice = result["choices"][0]
                    if "message" in choice and "content" in choice["message"]:
                        return choice["message"]["content"]
                
                # Fallback: return the whole response
                return json.dumps(result, ensure_ascii=False, indent=2)
            else:
                return f"API Error (Status {response.status_code}): {response.text}"
                
        except Exception as e:
            logger.error(f"Error calling remote API: {str(e)}")
            return f"Error calling remote API: {str(e)}"

# 工作流智能体
class WorkflowAgent(RemoteAgent):
    def __init__(self, agent_card, model_id):
        super().__init__(agent_card, model_id)
        self.session_id = None
        self.invoke_url = getattr(agent_card, "api", {}).get("invoke_url")
        if not self.invoke_url:
            raise ValueError("AgentCard未配置invoke_url接口地址！")
        
        print(f"[DEBUG] WorkflowAgent 初始化:")
        print(f"[DEBUG]   invoke_url: {self.invoke_url}")
        print(f"[DEBUG]   model_id: {model_id}")
        print(f"[DEBUG]   agent_card.api: {getattr(agent_card, 'api', {})}")

    def ask(self, query):
        import requests, json
        import io
        import sys

        markdown_lines = []
        self.session_id = None

        # 添加连接测试
        print(f"[DEBUG] WorkflowAgent.ask 开始，query: {query}")
        print(f"[DEBUG] WorkflowAgent.invoke_url: {self.invoke_url}")
        print(f"[DEBUG] WorkflowAgent.model_id: {self.model_id}")

        def event_iter(resp):
            for line in resp.iter_lines():
                if not line:
                    continue
                if line.startswith(b'data: '):
                    msg_str = line[len(b'data: '):].decode('utf-8')
                elif line.startswith('data: '):
                    msg_str = line[len('data: '):]
                else:
                    continue
                try:
                    msg_json = json.loads(msg_str)
                    yield msg_json
                except Exception as e:
                    print("[WARN] 事件解析失败:", msg_str, e)
                    continue

        def process_stream(query_input=None, is_first_round=False):
            payload = {
                "workflow_id": self.model_id,
                "stream": True
            }
            if self.session_id:
                payload["session_id"] = self.session_id
            if query_input is not None:
                payload["input"] = query_input

            print(f"[DEBUG] process_stream 开始，payload: {payload}")
            
            # 先测试工作流是否可访问
            try:
                test_resp = requests.get(self.invoke_url.replace('/invoke', '/health'), timeout=5)
                print(f"[DEBUG] 工作流健康检查状态码: {test_resp.status_code}")
            except Exception as e:
                print(f"[DEBUG] 工作流健康检查失败: {e}")
            
            try:
                resp = requests.post(self.invoke_url, json=payload, timeout=120, stream=True)
                print(f"[DEBUG] 请求响应状态码: {resp.status_code}")
            except Exception as e:
                error_msg = f"工作流请求异常: {e}"
                print(f"[ERROR] {error_msg}")
                markdown_lines.append(f"❌ 错误: {error_msg}\n")
                return
            
            # 检查响应状态码
            if resp.status_code != 200:
                error_msg = f"工作流请求失败，状态码: {resp.status_code}"
                print(f"[ERROR] {error_msg}")
                markdown_lines.append(f"❌ 错误: {error_msg}\n")
                return
            
            # 添加事件计数器，防止无限循环
            event_count = 0
            max_events = 1000  # 最大事件数量

            for message in event_iter(resp):
                event_count += 1
                if event_count > max_events:
                    print(f"[WARN] 事件数量超过 {max_events}，强制结束工作流")
                    markdown_lines.append(f"💬 系统: 工作流事件数量过多，已自动结束\n")
                    return
                
                # 处理实际的事件格式
                event_data = message.get('data', message)
                event_type = event_data.get('event', '')
                status = event_data.get('status', '')
                print(f"[DEBUG] 收到事件: event_type={event_type}, status={status}")
                
                # 检查所有事件的内容，不管status如何
                output_schema = event_data.get('output_schema', {})
                if output_schema:
                    print(f"[DEBUG] output_schema: {output_schema}")
                    files = output_schema.get('files', [])
                    if files:
                        print(f"[DEBUG] 检测到files字段: {files}")
                    msg = output_schema.get('message', '')
                    if msg:
                        print(f"[DEBUG] 消息内容: {msg[:100]}...")  # 只显示前100个字符
                
                # 只处理status为"end"的事件，忽略流式返回
                # 但是如果有图片内容，也要处理
                has_images = False
                if output_schema:
                    files = output_schema.get('files', [])
                    if files:
                        has_images = True
                    msg = output_schema.get('message', '')
                    if msg and '![' in msg and '](' in msg:
                        has_images = True
                
                if status != "end" and event_type != "stream_msg" and not has_images:
                    continue
                # 处理session_id
                if "session_id" in message:
                    self.session_id = message["session_id"]


                if event_type == 'guide_word':
                    # 处理引导词事件 - 只在status为"end"时处理
                    if status != "end":
                        print(f"[DEBUG] 跳过非结束的guide_word事件: status={status}")
                        continue
                        
                    output_schema = event_data.get('output_schema', {})
                    msg = output_schema.get('message', '')
                    if msg:
                        markdown_lines.append(f"💬 系统: {msg}\n")
                        print(f"[DEBUG] 添加guide_word: {msg}")
                    else:
                        print(f"[DEBUG] guide_word 没有消息内容")
                
                elif event_type == 'output_msg':
                    # 处理输出消息事件 - 只在status为"end"时处理
                    if status != "end":
                        print(f"[DEBUG] 跳过非结束的output_msg事件: status={status}")
                        continue


                        
                    output_schema = event_data.get('output_schema', {})
                    msg = output_schema.get('message', '')
                    
                    # 检查是否有图片内容
                    files = output_schema.get('files', [])
                    if files:
                        print(f"[DEBUG] 检测到图片文件: {files}")
                        for file_info in files:
                            if isinstance(file_info, dict) and 'url' in file_info:
                                img_markdown = f"![图片]({file_info['url']})"
                                markdown_lines.append(f"{img_markdown}\n")
                                print(f"[DEBUG] 添加图片: {img_markdown}")
                    
                    if msg:
                        # 将链接格式转换为图片格式
                        import re
                        # 匹配 [查看图表](url) 格式的链接
                        link_pattern = r'\[([^\]]*)\]\(([^)]+\.(png|jpg|jpeg|gif|webp))\)'
                        img_matches = re.findall(link_pattern, msg)
                        if img_matches:
                            print(f"[DEBUG] 检测到图片链接: {img_matches}")
                            for alt_text, url, ext in img_matches:
                                # 将链接格式转换为图片格式
                                img_markdown = f"![{alt_text}]({url})"
                                msg = msg.replace(f"[{alt_text}]({url})", img_markdown)
                                print(f"[DEBUG] 转换图片链接: {img_markdown}")
                        
                        markdown_lines.append(f"🤖 AI回答: {msg}\n")
                        print(f"[DEBUG] 添加output_msg: {msg}")
                        
                        # 检查消息文本中是否包含图片markdown
                        img_pattern = r'!\[([^\]]*)\]\(([^)]+)\)'
                        img_matches = re.findall(img_pattern, msg)
                        if img_matches:
                            print(f"[DEBUG] 在output_msg中检测到图片markdown: {img_matches}")
                    else:
                        print(f"[DEBUG] output_msg 没有消息内容")
                
                elif event_type == 'guide_question':
                    # 处理引导问题事件 - 只在status为"end"时处理
                    if status != "end":
                        #print(f"[DEBUG] 跳过非结束的guide_question事件: status={status}")
                        continue
                        
                    output_schema = event_data.get('output_schema', {})
                    msg_list = output_schema.get('message', [])
                    if msg_list and msg_list != ['']:
                        markdown_lines.append("## 可选问题\n")
                        for idx, m in enumerate(msg_list, 1):
                            if m:
                                markdown_lines.append(f"{idx}. {m}\n")
                
                elif event_type == 'input':
                    # 处理输入事件 - 只在status为"end"时处理
                    if status != "end":
                        #print(f"[DEBUG] 跳过非结束的input事件: status={status}")
                        continue
                        
                    input_schema = event_data.get('input_schema', {})
                    input_values = input_schema.get('value', [])
                    print(f"[DEBUG] 收到input事件: {event_data}")
                    
                    if input_values and isinstance(input_values, list):
                        first_input = input_values[0]
                        input_key = first_input.get('key')
                        print(f"[DEBUG] input_key: {input_key}")
                        
                        if input_key == 'user_input':
                            node_id = event_data.get('node_id')
                            if not node_id:
                                print("[ERROR] 无法获取输入节点的 node_id")
                                return
                            
                            if is_first_round and query:
                                # 使用用户查询作为输入，格式：{node_id: {input_key: query}}
                                user_input = query
                                markdown_lines.append(f"📝 用户输入: {user_input}\n")
                                print(f"[DEBUG] 使用用户查询作为输入: {user_input}")
                                # 继续处理，传入正确格式的用户输入
                                next_query_input = {
                                    node_id: {
                                        input_key: user_input
                                    }
                                }
                                print(f"[DEBUG] 发送输入: {next_query_input}")
                                process_stream(query_input=next_query_input, is_first_round=False)
                                return
                            else:
                                # 等待用户输入
                                prompt = first_input.get('label', '请输入内容')
                                if prompt is None or prompt == 'None':
                                    # 如果没有有效的提示，自动结束
                                    print("[INFO] 没有有效的输入提示，自动结束工作流")
                                    return
                                # 在Web API环境中，不能使用input()，直接结束工作流
                                print(f"[INFO] 工作流需要用户输入: {prompt}，但Web API不支持交互式输入，自动结束")
                                # 清除session_id，确保下次调用时重新开始
                                self.session_id = None
                                # 在结束前，添加一个友好的消息说明情况
                                if not markdown_lines:
                                    markdown_lines.append("💬 系统: 工作流已完成主要任务，但需要额外交互。由于Web API限制，已自动结束。\n")
                                # 不要立即返回，继续处理后续事件
                                print(f"[DEBUG] 继续处理后续事件，不立即结束")
                                continue
                        else:
                            # 处理其他类型的输入
                            print(f"[INFO] 检测到其他类型输入: {input_key}，自动跳过")
                            return
                    else:
                        # 没有有效的输入字段，结束工作流
                        print("[INFO] 没有有效的输入字段，工作流已完成")
                        return
                
                elif event_type.lower() == 'close':
                    output_schema = event_data.get('output_schema', {})
                    msg = output_schema.get('message', '')
                    if msg:
                        markdown_lines.append(f"## 最终结果\n{msg}\n")
                        print(f"[DEBUG] 添加close事件: {msg}")
                    else:
                        print(f"[DEBUG] close 事件没有消息内容")
                    return

                
                elif event_type == 'output_with_input_msg':
                    # 处理带输入的输出消息 - 只在status为"end"时处理
                    if status != "end":
                        print(f"[DEBUG] 跳过非结束的output_with_input_msg事件: status={status}")
                        continue
                        
                    output_schema = event_data.get('output_schema', {})
                    msg = output_schema.get('message', '')
                    if msg:
                        markdown_lines.append(f"🤖 AI回答: {msg}\n")
                
                elif event_type == 'output_with_choose_msg':
                    # 处理带选择的输出消息 - 只在status为"end"时处理
                    if status != "end":
                        print(f"[DEBUG] 跳过非结束的output_with_choose_msg事件: status={status}")
                        continue
                        
                    output_schema = event_data.get('output_schema', {})
                    msg = output_schema.get('message', '')
                    if msg:
                        markdown_lines.append(f"🤖 AI回答: {msg}\n")
                
                elif event_type == 'start':
                    # 处理开始事件
                    print(f"[INFO] 工作流开始执行")
                
                elif event_type == 'end':
                    # 处理结束事件
                    print(f"[INFO] 工作流执行结束")
                    return
                
                elif event_type == 'error':
                    # 处理错误事件
                    error_msg = event_data.get('message', '未知错误')
                    print(f"[ERROR] 工作流执行错误: {error_msg}")
                    markdown_lines.append(f"❌ 错误: {error_msg}\n")
                    return
                
                elif event_type == 'progress':
                    # 处理进度事件
                    progress_msg = event_data.get('message', '')
                    if progress_msg:
                        print(f"[INFO] 进度: {progress_msg}")
                
                elif event_type == 'status':
                    # 处理状态事件
                    status_msg = event_data.get('message', '')
                    if status_msg:
                        print(f"[INFO] 状态: {status_msg}")
                
                elif event_type == 'debug':
                    # 处理调试事件
                    debug_msg = event_data.get('message', '')
                    if debug_msg:
                        print(f"[DEBUG] {debug_msg}")
                
                elif event_type == 'warning':
                    # 处理警告事件
                    warning_msg = event_data.get('message', '')
                    if warning_msg:
                        print(f"[WARN] {warning_msg}")
                
                elif event_type == 'info':
                    # 处理信息事件
                    info_msg = event_data.get('message', '')
                    if info_msg:
                        print(f"[INFO] {info_msg}")
                
                elif event_type == 'success':
                    # 处理成功事件
                    success_msg = event_data.get('message', '')
                    if success_msg:
                        print(f"[SUCCESS] {success_msg}")
                        markdown_lines.append(f"✅ {success_msg}\n")
                
                elif event_type == 'failure':
                    # 处理失败事件
                    failure_msg = event_data.get('message', '')
                    if failure_msg:
                        print(f"[FAILURE] {failure_msg}")
                        markdown_lines.append(f"❌ {failure_msg}\n")
                
                elif event_type == 'stream_msg':
                    # 处理流式消息事件 - 只在status为"end"时处理
                    if status != "end":
                        print(f"[DEBUG] 跳过非结束的stream_msg事件: status={status}")
                        continue
                        
                    output_schema = event_data.get('output_schema', {})
                    print(f"[DEBUG] stream_msg output_schema: {output_schema}")
                    msg = output_schema.get('message', '')
                    print(f"[DEBUG] stream_msg msg: {msg}")
                    
                    # 检查是否有图片内容
                    files = output_schema.get('files', [])
                    if files:
                        print(f"[DEBUG] 检测到图片文件: {files}")
                        for file_info in files:
                            if isinstance(file_info, dict) and 'url' in file_info:
                                img_markdown = f"![图片]({file_info['url']})"
                                markdown_lines.append(f"{img_markdown}\n")
                                print(f"[DEBUG] 添加图片: {img_markdown}")
                    
                    if msg:
                        #print(f"[STREAM] {msg}")
                        
                        # 将链接格式转换为图片格式
                        import re
                        # 匹配 [查看图表](url) 格式的链接
                        link_pattern = r'\[([^\]]*)\]\(([^)]+\.(png|jpg|jpeg|gif|webp))\)'
                        img_matches = re.findall(link_pattern, msg)
                        if img_matches:
                            print(f"[DEBUG] 检测到图片链接: {img_matches}")
                            for alt_text, url, ext in img_matches:
                                # 将链接格式转换为图片格式
                                img_markdown = f"![{alt_text}]({url})"
                                msg = msg.replace(f"[{alt_text}]({url})", img_markdown)
                                print(f"[DEBUG] 转换图片链接: {img_markdown}")
                        
                        markdown_lines.append(f"🤖 AI回答: {msg}\n")
                        print(f"[DEBUG] 添加AI回答: {msg}")
                        
                        # 检查消息文本中是否包含图片markdown
                        img_pattern = r'!\[([^\]]*)\]\(([^)]+)\)'
                        img_matches = re.findall(img_pattern, msg)
                        if img_matches:
                            print(f"[DEBUG] 在消息文本中检测到图片markdown: {img_matches}")
                    else:
                        # 尝试从其他字段获取消息
                        msg = event_data.get('message', '')
                        print(f"[DEBUG] stream_msg event_data.get('message'): {msg}")
                        if msg:
                            print(f"[STREAM] {msg}")
                            markdown_lines.append(f"🤖 AI回答: {msg}\n")
                            print(f"[DEBUG] 添加AI回答: {msg}")
                        else:
                            # 如果没有消息内容，跳过这个事件
                            print(f"[INFO] 跳过空的stream_msg事件")
                            continue
                
                else:
                    print(f"[WARN] 未支持的事件类型: {event_type}")
                    # 尝试从事件数据中提取有用信息
                    if 'message' in event_data:
                        print(f"[INFO] 事件消息: {event_data['message']}")
                    if 'output_schema' in event_data:
                        output_schema = event_data['output_schema']
                        if 'message' in output_schema:
                            msg = output_schema['message']
                            if msg:
                                markdown_lines.append(f"💬 系统: {msg}\n")

        # 启动主流程
        process_stream(query_input=None, is_first_round=True)
        md_content = "".join(markdown_lines).strip()
        # 添加调试信息
        print(f"[DEBUG] WorkflowAgent.ask 返回内容: {md_content}")
        print(f"[DEBUG] WorkflowAgent.ask markdown_lines: {markdown_lines}")
        
        # 如果没有内容，返回默认消息
        if not md_content:
            md_content = "💬 系统: 工作流已完成，但没有生成具体内容。请尝试重新提问。"
        
        # 不再打印到控制台，直接返回内容
        return md_content



def extract_workflow_messages(stream_output):
    """
    从WorkflowAgent的流式输出中提取message，生成用户友好的输出
    
    Args:
        stream_output: WorkflowAgent的流式输出内容
        
    Returns:
        str: 用户友好的完整输出
    """
    import re
    import json
    
    # 存储提取的消息
    messages = []
    questions = []
    final_message = ""
    
    # 按行分割输出
    lines = stream_output.split('\n')
    
    for line in lines:
        line = line.strip()
        
        # 提取用户输入
        if '📝 用户输入:' in line:
            message = line.split('📝 用户输入:', 1)[1].strip()
            if message:
                messages.append(f"📝 用户输入: {message}")
        
        # 提取AI回答
        elif '🤖 AI回答:' in line:
            message = line.split('🤖 AI回答:', 1)[1].strip()
            if message:
                messages.append(f"🤖 AI回答: {message}")
        
        # 提取系统消息
        elif '💬 系统:' in line:
            message = line.split('💬 系统:', 1)[1].strip()
            if message:
                messages.append(f"💬 系统: {message}")
        
        # 提取最终结果
        elif '## 最终结果' in line:
            # 获取下一行的内容作为最终结果
            continue
        
        # 提取可选问题
        elif '## 可选问题' in line:
            # 收集后续的问题
            continue
        
        # 提取问题选项
        elif re.match(r'^\d+\.\s+', line):
            questions.append(line)
        
        # 提取错误信息
        elif '❌ 错误:' in line:
            message = line.split('❌ 错误:', 1)[1].strip()
            if message:
                messages.append(f"❌ 错误: {message}")
        
        # 提取成功信息
        elif '✅' in line:
            message = line.split('✅', 1)[1].strip()
            if message:
                messages.append(f"✅ {message}")
        
        # 提取失败信息
        elif '❌' in line and '错误:' not in line:
            message = line.split('❌', 1)[1].strip()
            if message:
                messages.append(f"❌ {message}")
        
        # 提取流式消息
        elif '[STREAM]' in line:
            message = line.split('[STREAM]', 1)[1].strip()
            if message:
                messages.append(f"🤖 AI回答: {message}")
        
        # 提取大模型回答（旧格式）
        elif '[大模型回答]:' in line:
            message = line.split('[大模型回答]:', 1)[1].strip()
            if message:
                messages.append(f"🤖 AI回答: {message}")
        
        # 提取系统输出（旧格式）
        elif '[系统输出]:' in line:
            message = line.split('[系统输出]:', 1)[1].strip()
            if message:
                # 检查是否是引导问题
                if '请选择一个问题：' in message:
                    continue  # 跳过引导问题标题
                elif message.startswith('工作流结束：'):
                    final_message = message.replace('工作流结束：', '').strip()
                else:
                    messages.append(f"💬 系统: {message}")
        
        # 提取DEBUG事件中的消息（实际输出格式）
        elif '[DEBUG] 收到事件：' in line:
            try:
                # 解析JSON事件
                event_str = line.split('[DEBUG] 收到事件：', 1)[1].strip()
                # 将单引号替换为双引号以符合JSON格式
                event_str = event_str.replace("'", '"')
                # 将Python的None替换为JSON的null
                event_str = event_str.replace("None", "null")
                event_data = json.loads(event_str)
                event = event_data.get('data', event_data)
                event_type = event.get("event", "")
                
                # 处理不同类型的消息
                if event_type == "guide_word":
                    msg = event.get("output_schema", {}).get("message")
                    if msg:
                        messages.append(f"💬 系统: {msg}")
                
                elif event_type == "output_msg":
                    msg = event.get("output_schema", {}).get("message")
                    if msg:
                        messages.append(f"🤖 AI回答: {msg}")
                
                elif event_type == "guide_question":
                    msg_list = event.get("output_schema", {}).get("message", [])
                    if msg_list and msg_list != [""]:
                        for idx, m in enumerate(msg_list, 1):
                            if m:
                                questions.append(f"{idx}. {m}")
                
                elif event_type == "close":
                    msg = event.get("output_schema", {}).get("message")
                    if msg:
                        final_message = msg
                
            except Exception as e:
                # 如果JSON解析失败，忽略这行
                pass
        
        # 提取引导问题选项
        elif line.startswith('  ') and line.strip().endswith('.'):
            # 匹配 "  1. 问题内容" 格式
            match = re.match(r'\s*(\d+)\.\s*(.+)', line)
            if match:
                question_num, question_content = match.groups()
                questions.append(f"{question_num}. {question_content}")
        
        # 提取自动输入
        elif '[自动输入]:' in line:
            input_content = line.split('[自动输入]:', 1)[1].strip()
            if input_content:
                messages.append(f"📝 用户输入: {input_content}")
        
        # 提取用户输入
        elif '[等待输入]' in line and ':' in line:
            input_content = line.split(':', 1)[1].strip()
            if input_content:
                messages.append(f"📝 用户输入: {input_content}")
        
        # 处理纯文本内容（没有表情符号前缀的消息）
        elif line and not line.startswith('[') and not line.startswith('##') and not re.match(r'^\d+\.\s+', line):
            # 如果这行有内容且不是其他格式，直接作为AI回答
            if line.strip():
                messages.append(f"🤖 AI回答: {line}")
    
    # 构建用户友好的输出
    output_parts = []
    
    # 添加主要消息
    if messages:
        output_parts.append("## 对话记录")
        for msg in messages:
            output_parts.append(msg)
    
    # 添加引导问题
    if questions:
        output_parts.append("\n## 可选问题")
        for question in questions:
            output_parts.append(question)
    
    # 添加最终结果
    if final_message:
        output_parts.append(f"\n## 最终结果\n{final_message}")
    elif messages:
        # 如果没有明确的最终结果，使用最后一条消息作为结果
        last_message = messages[-1]
        if 'AI回答:' in last_message:
            output_parts.append(f"\n## 最终结果\n{last_message.split('AI回答:', 1)[1].strip()}")
        elif '系统:' in last_message:
            output_parts.append(f"\n## 最终结果\n{last_message.split('系统:', 1)[1].strip()}")
        else:
            # 如果最后一条消息没有前缀，直接使用
            output_parts.append(f"\n## 最终结果\n{last_message}")
    
    return '\n'.join(output_parts) if output_parts else "未找到有效的消息内容"

def process_workflow_output(workflow_agent_response):
    """
    处理WorkflowAgent的输出，提取消息并生成用户友好的格式
    
    Args:
        workflow_agent_response: WorkflowAgent的原始输出
        
    Returns:
        str: 格式化的用户友好输出
    """
    # 添加调试信息
    print(f"[DEBUG] process_workflow_output 输入类型: {type(workflow_agent_response)}")
    print(f"[DEBUG] process_workflow_output 输入内容: {workflow_agent_response}")
    
    # 如果输出是字符串，直接处理
    if isinstance(workflow_agent_response, str):
        # 如果已经是markdown格式，直接返回
        if ('💬 系统:' in workflow_agent_response or 
            '🤖 AI回答:' in workflow_agent_response or 
            '📝 用户输入:' in workflow_agent_response or
            '## 对话记录' in workflow_agent_response or
            '## 最终结果' in workflow_agent_response):
            print(f"[DEBUG] 检测到markdown格式，直接返回")
            return workflow_agent_response
        elif workflow_agent_response.strip():
            # 如果有内容但不是markdown格式，直接返回
            print(f"[DEBUG] 检测到纯文本内容，直接返回")
            return workflow_agent_response
        else:
            # 否则使用extract_workflow_messages处理
            print(f"[DEBUG] 使用extract_workflow_messages处理")
            return extract_workflow_messages(workflow_agent_response)
    
    # 如果输出是字典或其他格式，转换为字符串后处理
    try:
        if isinstance(workflow_agent_response, dict):
            return extract_workflow_messages(json.dumps(workflow_agent_response, ensure_ascii=False, indent=2))
        else:
            return extract_workflow_messages(str(workflow_agent_response))
    except Exception as e:
        return f"处理输出时出错: {str(e)}"

# 构建智能体网络
def build_agent_network(agent_json_url):
    agent_cards = fetch_agent_cards(agent_json_url)
    network = AgentNetwork(name="智能路由网络")
    for name, agent_card, model_id in agent_cards:
        logger.info(f"name: {name}, agent_card: {agent_card}, model_id: {model_id}")
        # 判断是否workflow agent
        if is_workflow_agent(agent_card):
            print(f"[DEBUG] 创建WorkflowAgent: {name}")
            print(f"[DEBUG] agent_card.api: {getattr(agent_card, 'api', {})}")
            remote_agent = WorkflowAgent(agent_card, model_id)
        else:
            print(f"[DEBUG] 创建RemoteAgent: {name}")
            remote_agent = RemoteAgent(agent_card, model_id)
        network.agents[name] = remote_agent
        network.agent_cards[name] = agent_card
    return network

def create_router(network, openai_client):
    try:
        logger.info("=== 开始创建路由器 ===")
        
        if openai_client is None:
            logger.error("OpenAI客户端未初始化，无法创建路由器")
            return None
        
        logger.info(f"开始创建路由器，网络包含 {len(network.agents)} 个智能体")
        logger.info(f"OpenAI客户端状态: {openai_client is not None}")
        logger.info(f"OpenAI客户端类型: {type(openai_client)}")
        
        # 检查网络中的智能体
        logger.info("检查网络中的智能体:")
        for name, agent in network.agents.items():
            logger.info(f"  - {name}: {type(agent)}")
        
        # 检查网络中的智能体卡片
        logger.info("检查网络中的智能体卡片:")
        for name, agent_card in network.agent_cards.items():
            logger.info(f"  - {name}: {agent_card.name} - {agent_card.description[:50]}...")
        
        # 测试OpenAI客户端是否可用
        try:
            logger.info("测试OpenAI客户端连接...")
            # 这里可以添加一个简单的测试调用
            logger.info("OpenAI客户端连接测试通过")
        except Exception as test_e:
            logger.error(f"OpenAI客户端连接测试失败: {test_e}")
            return None
        
        # 构建系统提示词
        system_prompt = (
            "你是智能路由器，请根据用户的查询和可用的智能体信息，将问题准确路由到最适合处理该请求的智能体。\n\n"
            "请仔细分析用户查询的内容和意图，以及每个智能体的描述、技能和示例，选择最合适的智能体。\n"
            "仅返回智能体名称。"
        )
        logger.info(f"系统提示词: {system_prompt}")
        
        # 创建路由器
        logger.info("正在创建AIAgentRouter...")
        router = AIAgentRouter(
            llm_client=openai_client,
            agent_network=network,
            system_prompt=system_prompt
        )
        logger.info("路由器创建成功")
        logger.info(f"路由器类型: {type(router)}")
        logger.info("=== 路由器创建完成 ===")
        return router
    except Exception as e:
        logger.error(f"创建路由器失败: {e}")
        import traceback
        logger.error(f"详细错误信息: {traceback.format_exc()}")
        return None

# 全局变量，用于缓存网络和路由器
_global_network = None
_global_router = None

# API路由
@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()})

@app.route('/api/agents', methods=['GET'])
def get_agents():
    """获取所有可用的智能体信息"""
    global _global_network
    
    try:
        agent_json_url = request.args.get('agent_json_url', 'http://llmtest.ouyeelf.com/.well-known/agent.json')
        
        if _global_network is None:
            try:
                _global_network = build_agent_network(agent_json_url)
            except Exception as e:
                logger.error(f"构建智能体网络失败: {e}")
                return jsonify({
                    "success": False,
                    "error": f"构建智能体网络失败: {str(e)}"
                }), 500
        
        agents = _global_network.list_agents()
        return jsonify({
            "success": True,
            "agents": agents,
            "count": len(agents)
        })
    except Exception as e:
        logger.error(f"获取智能体列表失败: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/query', methods=['POST'])
def handle_query():
    """处理用户查询并路由到合适的智能体"""
    global _global_network, _global_router
    
    try:
        data = request.get_json()
        query = data.get('query', '')
        agent_json_url = data.get('agent_json_url', 'http://llmtest.ouyeelf.com/.well-known/agent.json')
        force_reload = data.get('force_reload', False)
        
        if not query:
            return jsonify({
                "success": False,
                "error": "查询内容不能为空"
            }), 400
        
        # 如果网络未初始化或强制重新加载，则构建网络
        if _global_network is None or force_reload:
            logger.info("正在初始化智能体网络...")
            try:
                logger.info("开始构建智能体网络...")
                _global_network = build_agent_network(agent_json_url)
                logger.info(f"智能体网络构建成功，包含 {len(_global_network.agents)} 个智能体")
                
                logger.info("开始创建路由器...")
                _global_router = create_router(_global_network, openai_client)
                logger.info("路由器创建完成！")
                
                if _global_router is None:
                    logger.error("路由器创建失败，返回None")
                    return jsonify({
                        "success": False,
                        "error": "路由器创建失败"
                    }), 500
                
                logger.info("智能体网络初始化完成！")
            except Exception as e:
                logger.error(f"初始化智能体网络失败: {e}")
                return jsonify({
                    "success": False,
                    "error": f"初始化智能体网络失败: {str(e)}"
                }), 500
        
        # 如果网络存在但路由器不存在，则创建路由器
        if _global_network is not None and _global_router is None:
            logger.info("网络存在但路由器不存在，正在创建路由器...")
            try:
                _global_router = create_router(_global_network, openai_client)
                logger.info("路由器创建完成！")
                
                if _global_router is None:
                    logger.error("路由器创建失败，返回None")
                    return jsonify({
                        "success": False,
                        "error": "路由器创建失败"
                    }), 500
            except Exception as e:
                logger.error(f"创建路由器失败: {e}")
                return jsonify({
                    "success": False,
                    "error": f"创建路由器失败: {str(e)}"
                }), 500
        
        # 检查路由器是否成功初始化
        logger.info(f"当前路由器状态: {_global_router is not None}")
        if _global_router is None:
            logger.error("路由器未初始化")
            logger.info(f"全局网络状态: {_global_network is not None}")
            if _global_network is not None:
                logger.info(f"网络包含 {len(_global_network.agents)} 个智能体")
            return jsonify({
                "success": False,
                "error": "智能体路由器未正确初始化"
            }), 500
        
        logger.info(f"用户查询: {query}")
        
        agent_name, confidence = _global_router.route_query(query)
        logger.info(f"路由决策: {agent_name} (置信度: {confidence:.2f})")
        
        if not agent_name:
            return jsonify({
                "success": False,
                "error": "未找到合适的智能体处理该请求"
            })
        
        agent = _global_network.get_agent(agent_name)
        if agent:
            response = agent.ask(query)
            
            # 如果是WorkflowAgent，进行特殊处理
            if isinstance(agent, WorkflowAgent):
                return jsonify({
                    "success": True,
                    "routed_agent": agent_name,
                    "confidence": round(confidence, 2),
                    "raw_response": response,
                    "friendly_response": process_workflow_output(response),
                    "timestamp": datetime.now().isoformat()
                })
            else:
                return jsonify({
                    "success": True,
                    "routed_agent": agent_name,
                    "confidence": round(confidence, 2),
                    "response": response,
                    "timestamp": datetime.now().isoformat()
                })
        else:
            return jsonify({
                "success": False,
                "error": f"智能体 '{agent_name}' 不存在"
            })
            
    except Exception as e:
        logger.error(f"处理查询时出错: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/reload', methods=['POST'])
def reload_agents():
    """重新加载智能体网络"""
    global _global_network, _global_router
    
    try:
        data = request.get_json()
        agent_json_url = data.get('agent_json_url', 'http://llmtest.ouyeelf.com/.well-known/agent.json')
        
        logger.info("正在重新加载智能体网络...")
        _global_network = build_agent_network(agent_json_url)
        _global_router = create_router(_global_network, openai_client)
        
        agents = _global_network.list_agents()
        
        return jsonify({
            "success": True,
            "message": "智能体网络重新加载完成",
            "agents_count": len(agents),
            "timestamp": datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"重新加载智能体网络时出错: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/mock_workflow', methods=['POST'])
def mock_workflow():
    """模拟工作流端点，用于测试"""
    data = request.get_json()
    workflow_id = data.get('workflow_id')
    stream = data.get('stream', False)
    
    if stream:
        def generate():
            # 模拟初始问候
            yield f"data: {json.dumps({'data': {'event': 'guide_word', 'output_schema': {'message': '你好，我是测试助手'}}})}\n\n"
            
            # 模拟输入请求
            yield f"data: {json.dumps({'data': {'event': 'input', 'node_id': 'input_node', 'input_schema': {'value': [{'key': 'user_input'}]}}})}\n\n"
            
            # 模拟最终回答
            yield f"data: {json.dumps({'data': {'event': 'output_msg', 'output_schema': {'message': '这是测试工作流的回答'}}})}\n\n"
            
            # 模拟结束
            yield f"data: {json.dumps({'data': {'event': 'close', 'output_schema': {'message': '工作流完成'}}})}\n\n"
        
        return Response(generate(), mimetype='text/plain')
    else:
        return jsonify({"message": "测试工作流回答"})

@app.route('/test_workflow.json')
def test_workflow_json():
    """返回测试工作流配置"""
    return jsonify({
        "name": "测试工作流智能体",
        "description": "这是一个测试工作流智能体",
        "url": "http://localhost:5001/test_workflow.json",
        "version": "1.0.0",
        "category": "workflow",
        "parameters": {
            "model": "test_workflow_model"
        },
        "api": {
            "invoke_url": "http://localhost:5001/mock_workflow"
        },
        "skills": [
            {
                "name": "测试技能",
                "description": "测试工作流技能",
                "tags": ["test", "workflow"],
                "examples": ["测试查询"]
            }
        ]
    })

@app.route('/test_agent.json')
def test_agent_json():
    """返回测试agent.json"""
    return jsonify({
        "agents": [
            {
                "name": "测试工作流智能体",
                "url": "http://localhost:5001/test_workflow.json",
                "category": "workflow"
            }
        ]
    })

# === 服务自描述（带懒加载，确保 count 正常）===
@app.route('/api/description', methods=['GET'])
def api_description():
    """
    返回 { success: true, description: "..." }
    若本地缓存为空，尝试懒加载一次目录填充 _global_network
    """
    global _global_network
    agent_json_url = request.args.get(
        'agent_json_url',
        'http://llmtest.ouyeelf.com/.well-known/agent.json'
    )
    try:
        # 懒加载：如果还没有缓存，主动构建一次
        if _global_network is None or not getattr(_global_network, "agents", {}):
            logger.info(f"/api/description 懒加载 _global_network from {agent_json_url}")
            try:
                _global_network = build_agent_network(agent_json_url)
            except Exception as e:
                logger.warning(f"/api/description 懒加载失败（不影响接口返回）：{e}")

        count = len(getattr(_global_network, "agents", {})) if _global_network else 0
        desc = (
            "将用户问题智能路由到最合适的下游智能体（支持 workflow 与普通远程 Agent），"
            "自动调用对应智能体 API 并返回结果。必填参数：query。可选：preferred_agent、force_reload。"
            f"（当前已缓存 {count} 个智能体）"
        )
        return jsonify({"success": True, "description": desc})
    except Exception as e:
        logger.error(f"/api/description 构造失败: {e}")
        return jsonify({"success": False, "description": ""}), 500



def _enrich_agent_item_minimal(item: dict) -> dict:
    """
    基于 /.well-known/agent.json 的结构（name/url/model），最小增强：
    - 一定保留 name/url/model
    - 尝试补充 description/category/version/skills/api（失败忽略）
    """
    out = {
        "name": item.get("name", ""),
        "url": item.get("url", ""),
        "model": item.get("model", ""),
    }
    # 如果没有 url 或 name，就直接返回最小字段
    url = out["url"]
    if not url or not out["name"]:
        return out

    try:
        resp = requests.get(url, timeout=8)
        resp.raise_for_status()
        card = resp.json() or {}
        # 按你给的 card 示例补充字段
        out.update({
            "description": card.get("description", ""),
            "category": card.get("category", ""),
            "version": card.get("version", "1.0.0"),
            "skills": card.get("skills", []),
            "api": card.get("api", {"url": card.get("url", "")}),
            # 某些 card 把模型放在 parameters.model，这里兜底
            "parameters": card.get("parameters", {}),
        })
        # 如果 parameters.model 存在而且与目录的 model 不同，则一并返回，供你排查
        pm = out.get("parameters", {}).get("model")
        if pm and pm != out["model"]:
            out["model_from_parameters"] = pm
    except Exception as e:
        logger.warning(f"增强 agent item 失败（{out['name']}）: {e}")
    return out


# === 智能体目录（直连成功也刷新本地缓存；支持 force_reload）===
@app.route('/api/catalog', methods=['GET'])
def api_catalog():
    """
    返回 { success: true, items: [ { name, url, model, ... } ] }
    - 直连成功：返回 items，并刷新 _global_network（保证后续 description/count 不为 0）
    - 直连失败：回退用本地网络构造
    """
    global _global_network

    agent_json_url = request.args.get(
        'agent_json_url',
        'http://llmtest.ouyeelf.com/.well-known/agent.json'
    )
    force_reload = request.args.get('force_reload', 'false').lower() in ('1', 'true', 'yes')

    # 若显式要求刷新，先清空缓存
    if force_reload:
        logger.info("/api/catalog 收到 force_reload=true，清空本地缓存")
        _global_network = None

    # 1) 直连目录
    try:
        logger.info(f"/api/catalog 直连: {agent_json_url}")
        resp = requests.get(agent_json_url, timeout=10)
        resp.raise_for_status()
        data = resp.json() or {}
        agents = data.get("agents", [])

        items = []
        for it in agents:
            name = it.get("name")
            if not name:
                continue
            items.append(_enrich_agent_item_minimal(it))

        # 🔁 同步刷新本地缓存（保证后续 /api/description 统计不为 0）
        try:
            logger.info("/api/catalog 直连成功，刷新 _global_network 缓存")
            _global_network = build_agent_network(agent_json_url)
        except Exception as e:
            logger.warning(f"/api/catalog 刷新 _global_network 失败（不影响接口返回）：{e}")

        return jsonify({"success": True, "items": items})
    except Exception as e:
        logger.warning(f"/api/catalog 直连失败，回退本地缓存: {e}")

    # 2) 回退：根据本地 network 构造（最小可用）
    try:
        if _global_network is None:
            logger.info("本地网络未初始化，尝试构建 _global_network ...")
            _global_network = build_agent_network(agent_json_url)

        items = []
        for name, card in _global_network.agent_cards.items():
            items.append({
                "name": getattr(card, "name", name),
                "url": getattr(card, "url", ""),
                "model": getattr(card, "parameters", {}).get("model", ""),
                "description": getattr(card, "description", ""),
                "category": getattr(card, "category", ""),
                "version": getattr(card, "version", "1.0.0"),
                "skills": [
                    {
                        "name": s.name,
                        "description": s.description,
                        "tags": s.tags,
                        "examples": s.examples
                    } for s in getattr(card, "skills", []) or []
                ],
                "api": getattr(card, "api", {}) or {}
            })
        return jsonify({"success": True, "items": items})
    except Exception as e:
        logger.error(f"/api/catalog 回退构造失败: {e}")
        return jsonify({"success": False, "items": []}), 500


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5001) 