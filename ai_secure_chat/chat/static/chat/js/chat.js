/**
 * 流式对话核心JS
 * @param {number} chatId 对话ID
 * @param {string} message 用户输入的消息
 * @param {function} onChunk 接收流式片段回调
 * @param {function} onComplete 完成回调
 * @param {function} onError 错误回调
 */
function sendStreamMessage(chatId, message, onChunk, onComplete, onError) {
    // 1. 构建表单数据（解决POST传参问题）
    const formData = new FormData();
    formData.append('message', message);

    // 2. 使用 fetch 发送POST请求（EventSource不支持POST）
    fetch(`/api/chat-stream/${chatId}/`, {
        method: 'POST',
        body: formData,
        headers: {
            'X-Requested-With': 'XMLHttpRequest',
        }
    })
    .then(response => {
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let fullText = '';

        // 3. 解析流式SSE数据
        function processStream() {
            return reader.read().then(({ done, value }) => {
                if (done) {
                    onComplete(fullText);
                    return;
                }

                // 解码数据
                const chunk = decoder.decode(value, { stream: true });
                // 按行解析SSE
                const lines = chunk.split('\n');
                lines.forEach(line => {
                    if (line.startsWith('data: ')) {
                        try {
                            const data = JSON.parse(line.replace('data: ', ''));
                            // 错误处理
                            if (data.error) {
                                onError(data.error);
                                return;
                            }
                            // 结束信号
                            if (data.done) return;
                            // 拼接内容
                            fullText += data.content;
                            onChunk(data.content);
                        } catch (e) {}
                    }
                });
                return processStream();
            });
        }
        processStream();
    })
    .catch(err => onError('网络连接失败'));
}

// ====================== 使用示例 ======================
// 绑定发送按钮
document.getElementById('send-btn').addEventListener('click', function() {
    const chatId = 1; // 从页面动态获取对话ID
    const input = document.getElementById('chat-input');
    const message = input.value.trim();

    if (!message) return;

    // 清空输入框
    input.value = '';
    const aiReplyDom = document.getElementById('ai-reply');
    aiReplyDom.innerHTML = ''; // 清空回复区域

    // 发送流式消息
    sendStreamMessage(
        chatId,
        message,
        // 流式接收片段（实时渲染）
        (chunk) => {
            aiReplyDom.innerHTML += chunk;
        },
        // 完成回调
        (fullText) => {
            console.log("对话完成:", fullText);
        },
        // 错误回调
        (err) => {
            aiReplyDom.innerHTML = `<span style="color:red;">错误：${err}</span>`;
        }
    );
});