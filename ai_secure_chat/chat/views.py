from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.urls import reverse
from .models import Category, Folder, ChatEntry, UserProfile, ChatMessage
from .forms import (
    CategoryForm, FolderForm, ChatEntryForm,
    PrivacyPasswordVerifyForm, #UserProfileForm
)
# 保留你已有的流式对话视图
from django.http import StreamingHttpResponse
# ✅ 新的类化 LLM 服务层：工厂 + 统一异常
# 视图不再直接绑定"千问"具体实现，换厂商时此处完全无需修改。
from .services import get_llm_provider, LLMError
import json
import time
import sys
import itertools
from urllib.parse import urlparse


# ========== 诊断用：统计 chat_stream 被调用了多少次 ==========
# 用途：如果前端一次点击触发了 2 次 fetch，这里序号会连增 2；
# 如果 Django 只进来 1 次，序号只+1。runserver 是单进程 + StatReloader
# 多线程的，全局 counter 足以区分"一次点击到底走进几次视图"。
_CHAT_STREAM_ENTER_COUNTER = itertools.count(1)


# ==============================================
# ✅ 流式对话视图（SSE）
# 关键点：
# 1. content_type="text/event-stream"，浏览器 fetch+ReadableStream 能逐块读
# 2. 响应头 X-Accel-Buffering:no、Cache-Control:no-cache 关掉所有代理/中间件缓冲
# 3. 生成器第一句先 yield 一个 ": ping\n\n" SSE 注释把 WSGI 缓冲顶开，
#    让浏览器立刻拿到第一批字节，触发 reader.read()
# 4. 所有异常在 services.iter_qwen_stream_text 内部已转为 ("error", msg)
#    再转成 SSE 事件喂给前端，不会出现"200 + 空 body"
# 5. 完整 AI 回复在流结束后 **一次性** 落库，避免边流边写 DB 的事务问题
# ==============================================
@login_required
@require_POST
def chat_stream(request, chat_id):
    _enter_seq = next(_CHAT_STREAM_ENTER_COUNTER)
    sys.stdout.write(
        f"\n>>>>> [chat_stream ENTER #{_enter_seq}] "
        f"chat_id={chat_id} user_id={request.user.id} "
        f"ts={time.strftime('%H:%M:%S')} "
        f"msg_len={len(request.POST.get('message', ''))}\n"
    )
    sys.stdout.flush()

    user_message = request.POST.get("message", "").strip()
    chat_entry = get_object_or_404(ChatEntry, id=chat_id, user=request.user)

    if not user_message:
        def _empty():
            yield "data: " + json.dumps({"error": "消息不能为空"}) + "\n\n"
            yield "event: done\ndata: {}\n\n"
        resp = StreamingHttpResponse(_empty(), content_type="text/event-stream; charset=utf-8")
        resp["Cache-Control"] = "no-cache"
        resp["X-Accel-Buffering"] = "no"
        return resp

    # 先把用户消息落库（非流式、瞬时完成）
    ChatMessage.objects.create(chat_entry=chat_entry, role="user", content=user_message)

    # 在进入生成器前取一次 provider：让 UserProfile / API Key 之类的
    # 配置错误能"同步"地转成 SSE error 事件，而不是在生成器里抛异常。
    try:
        provider = get_llm_provider(request.user)
        provider_error = None
    except LLMError as e:
        provider = None
        provider_error = str(e)
    except Exception as e:
        provider = None
        provider_error = f"客户端初始化失败：{e}"

    def sse_generator():
        # 0) 顶开 WSGI/中间件缓冲：一条 SSE 注释，浏览器会忽略但会触发 reader.read
        yield ": ping\n\n"
        sys.stdout.write("[chat_stream] 已发送首条 ping\n"); sys.stdout.flush()

        # 1) provider 初始化失败（未填 Key / Key 无效 / 厂商不支持等）
        if provider is None:
            sys.stdout.write(f"[chat_stream] provider 初始化失败: {provider_error}\n"); sys.stdout.flush()
            yield "data: " + json.dumps({"error": provider_error}, ensure_ascii=False) + "\n\n"
            yield "event: done\ndata: " + json.dumps({"ok": False}) + "\n\n"
            return

        full_text = ""
        try:
            # 2) 面向接口调用：任何厂商的 Provider 都按 (event_type, payload) 事件流对外
            for event_type, payload in provider.stream_chat(chat_entry, user_message):
                if event_type == "delta":
                    full_text += payload
                    yield "data: " + json.dumps({"content": payload}, ensure_ascii=False) + "\n\n"
                elif event_type == "error":
                    sys.stdout.write(f"[chat_stream] error: {payload}\n"); sys.stdout.flush()
                    yield "data: " + json.dumps({"error": payload}, ensure_ascii=False) + "\n\n"
                elif event_type == "done":
                    if payload:  # payload 为完整文本（若生成器成功）
                        full_text = payload
                    break
        except Exception as e:
            # 最后兜底，理论上 provider.stream_chat 内部已经把异常转成 error 事件
            yield "data: " + json.dumps({"error": f"服务器异常：{e}"}, ensure_ascii=False) + "\n\n"

        # 流结束后保存 AI 回复
        # 若 RAG 检索到了来源，先通过 SSE 推给前端再落库
        try:
            if full_text.strip():
                rag_source = getattr(provider, "_rag_last_source", None)
                if rag_source:
                    source_text = f"\n\n> 知识库来源：{rag_source}"
                    full_text = full_text.rstrip() + source_text
                    yield "data: " + json.dumps({"content": source_text}, ensure_ascii=False) + "\n\n"
                ChatMessage.objects.create(chat_entry=chat_entry, role="assistant", content=full_text)
        except Exception as e:
            sys.stdout.write(f"[chat_stream] 保存 AI 消息失败: {e}\n"); sys.stdout.flush()

        # 显式结束事件，前端据此关闭 reader
        yield "event: done\ndata: " + json.dumps({"ok": True}) + "\n\n"
        sys.stdout.write("[chat_stream] 流式结束\n"); sys.stdout.flush()

    resp = StreamingHttpResponse(sse_generator(), content_type="text/event-stream; charset=utf-8")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    # 关掉 gzip/压缩中间件对该响应的处理
    resp["Content-Encoding"] = "identity"
    return resp

@login_required
@require_POST
def chat_send(request, chat_id):
    """
    普通同步视图：一次性发送消息，接收完整AI回复
    无流式、无生成器、无阻塞、无事务问题
    """
    try:
        # 1. 获取参数
        user_message = request.POST.get("message", "").strip()
        if not user_message:
            return JsonResponse({"status": "error", "msg": "消息不能为空"}, status=400)

        # 2. 获取对话（视图内执行，无事务阻塞）
        chat_entry = get_object_or_404(ChatEntry, id=chat_id, user=request.user)

        # 3. 保存用户消息（立即提交，无锁表）
        ChatMessage.objects.create(
            chat_entry=chat_entry,
            role="user",
            content=user_message
        )

        # 4. 调用 LLM → 获取【完整回复】（一次性调用）
        #    通过工厂拿到当前用户配置的 Provider；换厂商此处零修改。
        provider = get_llm_provider(request.user)
        ai_reply = provider.chat(chat_entry, user_message)

        # 5. 追加 RAG 来源（与流式视图逻辑一致）
        rag_source = getattr(provider, "_rag_last_source", None)
        if rag_source:
            ai_reply = ai_reply.rstrip() + f"\n\n> 知识库来源：{rag_source}"

        # 6. 保存AI回复
        ChatMessage.objects.create(
            chat_entry=chat_entry,
            role="assistant",
            content=ai_reply
        )

        # 7. 一次性返回完整数据给前端
        return JsonResponse({
            "status": "success",
            "ai_content": ai_reply
        })

    except Exception as e:
        # 全局错误捕获
        return JsonResponse({
            "status": "error",
            "msg": f"服务器错误：{str(e)}"
        }, status=500)

# ==============================================
# 1. 分类列表视图 ✅【Claude优化版：加载关联文件夹】
# ==============================================
@login_required
def category_list(request):
    """分类列表 - 显示分类及其下的所有文件夹"""
    categories = Category.objects.filter(
        user=request.user
    ).prefetch_related('folders').order_by('order', '-created_at')
    return render(request, 'chat/category_list.html', {'categories': categories})

# ==============================================
# 2. 分类增删改查（无修改，保留原有）
# ==============================================
@login_required
def category_create(request):
    if request.method == 'POST':
        form = CategoryForm(request.POST)
        if form.is_valid():
            category = form.save(commit=False)
            category.user = request.user
            category.save()
            messages.success(request, '分类创建成功！')
            return redirect('chat:category_list')
    else:
        form = CategoryForm()
    return render(request, 'chat/category_form.html', {'form': form, 'title': '创建分类'})

@login_required
def category_update(request, pk):
    category = get_object_or_404(Category, pk=pk, user=request.user)
    if request.method == 'POST':
        form = CategoryForm(request.POST, instance=category)
        if form.is_valid():
            form.save()
            messages.success(request, '分类更新成功！')
            return redirect('chat:category_list')
    else:
        form = CategoryForm(instance=category)
    return render(request, 'chat/category_form.html', {'form': form, 'title': '编辑分类'})

@login_required
def category_delete(request, pk):
    category = get_object_or_404(Category, pk=pk, user=request.user)
    category.delete()
    messages.success(request, '分类删除成功！')
    return redirect('chat:category_list')

# ==============================================
# 3. 文件夹列表视图 ✅【Claude优化版：分类过滤+子文件夹+对话】
# ==============================================
@login_required
def folder_list(request, category_id=None):
    """文件夹列表 - 支持按分类过滤，显示子文件夹和对话条目"""

    if category_id:
        # 按分类筛选文件夹
        folders = Folder.objects.filter(
            user=request.user,
            category_id=category_id,
            parent_folder__isnull = True
        ).prefetch_related('child_folders', 'chat_entries').order_by('order', '-created_at')
        category = get_object_or_404(Category, id=category_id, user=request.user)
    else:
        # 只显示顶级文件夹（无父文件夹）
        folders = Folder.objects.filter(
            user=request.user,
            parent_folder__isnull=True
        ).prefetch_related('child_folders', 'chat_entries').order_by('order', '-created_at')
        category = None

    return render(request, 'chat/folder_list.html', {
        'folders': folders,
        'category': category
    })


@login_required
def folder_detail(request, category_id, folder_id):
    # 1. 校验分类权限
    category = get_object_or_404(Category, id=category_id, user=request.user)

    # 2. 校验当前文件夹权限
    parent_folder = get_object_or_404(
        Folder,
        id=folder_id,
        user=request.user,
        category=category
    )

    # 3. 查询子文件夹
    subfolders = Folder.objects.filter(
        parent_folder=parent_folder,
        user=request.user,
        category=category
    )

    # 4. 🔥 修复：标准查询当前文件夹下的所有对话（无歧义、无报错）
    chat_entries = ChatEntry.objects.filter(
        folder=parent_folder,
        user=request.user,
        # category=category
    ).order_by('-created_at')

    return render(request, 'chat/folder_detail.html', {
        'category': category,
        'parent_folder': parent_folder,
        'subfolders': subfolders,
        'chat_entries': chat_entries,
        'page_title': f'文件夹 - {parent_folder.name}'
    })

# ==============================================
# 4. 文件夹增删改查（无修改，保留原有）
# ==============================================
@login_required
def folder_create(request):
    if request.method == 'POST':
        form = FolderForm(request.POST, user=request.user)
        if form.is_valid():
            folder = form.save(commit=False)
            folder.user = request.user
            folder.save()
            messages.success(request, '文件夹创建成功！')
            # 如果为根文件夹，就重定向至folder_list.如果不是,重定向至folder_detail
            if folder.parent_folder:
                return redirect('chat:folder_detail',folder.category_id,folder.id)
            else:
                return redirect('chat:folder_list',folder.category_id)
    else:
        form = FolderForm(user=request.user)
    return render(request, 'chat/folder_form.html', {'form': form, 'title': '创建文件夹'})

@login_required
def folder_update(request, pk):
    folder = get_object_or_404(Folder, pk=pk, user=request.user)
    if request.method == 'POST':
        form = FolderForm(request.POST, user=request.user, instance=folder)
        if form.is_valid():
            folder = form.save(commit=False)
            folder.save()
            messages.success(request, '文件夹更新成功！')
            if folder.parent_folder:
                return redirect('chat:folder_detail',folder.category_id,folder.id)
            else:
                return redirect('chat:folder_list',folder.category_id)
    else:
        form = FolderForm(user=request.user, instance=folder)
    return render(request, 'chat/folder_form.html', {'form': form, 'title': '编辑文件夹'})

@login_required
def folder_delete(request, pk):
    folder = get_object_or_404(Folder, pk=pk, user=request.user)
    folder.delete()
    messages.success(request, '文件夹删除成功！')
    return redirect('chat:folder_list')

# ==============================================
# 5. 对话条目列表视图 ✅【Claude优化版：按文件夹过滤+上下文】
# ==============================================
@login_required
def chat_entry_list(request, folder_id=None):
    """对话列表 - 支持按文件夹筛选，显示所属文件夹信息"""
    if folder_id:
        # 按文件夹筛选对话
        folder = get_object_or_404(Folder, id=folder_id, user=request.user)
        chat_entries = ChatEntry.objects.filter(
            user=request.user,
            folder_id=folder_id
        ).order_by('-updated_at')
    else:
        # 显示所有对话
        folder = None
        chat_entries = ChatEntry.objects.filter(
            user=request.user
        ).order_by('-updated_at')

    return render(request, 'chat/chat_entry_list.html', {
        'chat_entries': chat_entries,
        'folder': folder
    })

@login_required
def chat_verify_privacy(request, chat_id):
    """独立的隐私密码验证页面，强制重定向访问"""
    chat_entry = get_object_or_404(ChatEntry, id=chat_id, user=request.user)
    profile = get_object_or_404(UserProfile, user=request.user)
    temp_session_key = f'private_chat_once_{chat_id}'

    # 已验证直接返回信息页
    # if request.session.get(session_key):
        # return redirect('chat:chat_entry_info', chat_id=chat_id)

    if request.method == 'POST':
        form = PrivacyPasswordVerifyForm(request.POST)
        if form.is_valid():
            pwd = form.cleaned_data['privacy_password']
            # 验证密码（你模型中的哈希验证方法）
            if profile.check_privacy_password(pwd):
                # ✅ 设置一次性验证标记
                request.session[temp_session_key] = True
                # 跳回info页，本次允许访问
                return redirect('chat:chat_entry_info', chat_id=chat_id)
            form.add_error('privacy_password', '密码错误，请重试！')
    else:
        form = PrivacyPasswordVerifyForm()

    return render(request, 'chat/private_verify.html', {
        'form': form,
        'chat_entry': chat_entry
    })

# ====================== 修正：对话信息页（隐私校验=重定向到验证URL）======================
@login_required
def chat_entry_info(request, chat_id):
    chat_entry = get_object_or_404(ChatEntry, id=chat_id, user=request.user)
    # 一次性验证标记（仅本次访问有效）
    temp_session_key = f'private_chat_once_{chat_id}'

    # ========== 隐私对话校验 ==========
    # 放行条件（满足其一即可）：
    #   1) session 里有刚输过密码留下的一次性 token → 消费后放行
    #   2) 请求的 Referer 指向同一对话的 chat_detail → 视作"从对话页返回信息页"，放行
    # 两条都不满足 → 跳转到密码验证页
    # 这样用户在 chat_detail 与 chat_entry_info 之间来回切换无需重复输入密码；
    # 但只要离开 info 去了其它页面再回来，Referer 不再是 chat_detail，就会重新要求密码。
    if chat_entry.is_private:
        if request.session.get(temp_session_key, False):
            # 一次性 token 消费，下次再来直接输密码
            del request.session[temp_session_key]
        else:
            referer_path = urlparse(request.META.get('HTTP_REFERER', '')).path
            detail_path = reverse('chat:chat_detail', args=[chat_id])
            # 用 endswith 兼容 i18n_patterns 带语言前缀的 URL（如 /zh-hans/...）
            came_from_detail = bool(referer_path) and referer_path.endswith(detail_path)
            if not came_from_detail:
                return redirect('chat:chat_verify_privacy', chat_id=chat_id)
            # 来自 chat_detail，免密放行；不写入任何 session 状态

    # 标记：从info页准备进入对话（用于chat_detail权限校验）
    request.session[f'from_info_{chat_id}'] = True

    context = {
        # 核心对象
        'chat_entry': chat_entry,
        # 关键字（Mezzanine格式）
        'keywords': chat_entry.keywords.all(),
        # 关联文件夹
        'folder': chat_entry.folder,
        # 基础信息
        'title': chat_entry.title,
        'description': chat_entry.description,
        'is_private': chat_entry.is_private,
        # 模型参数
        'temperature': chat_entry.temperature,
        'top_p': chat_entry.top_p,
        'max_tokens': chat_entry.max_tokens,
        # 时间信息（格式化）
        'created_at': chat_entry.created_at,
        'updated_at': chat_entry.updated_at if hasattr(chat_entry, 'updated_at') else None, # 避免updated_at属性不存在导致的报错
        # 页面配置
        'page_title': f"对话详情 - {chat_entry.title}",
        }

    return render(request, 'chat/chat_entry_info.html', context)


# ==============================================
# 6. 对话条目增删改查（无修改，保留原有）
# ==============================================
@login_required
def chat_entry_create(request):
    if request.method == 'POST':
        form = ChatEntryForm(request.POST, user=request.user)
        if form.is_valid():
            chat_entry = form.save(commit=False)
            chat_entry.user = request.user
            if chat_entry.use_rag:
                chat_entry.system_prompt='''
                你是资深金融投研分析师，仅基于已绑定的「金融投研知识库」中的上市公司财报、研报、招股书内容，输出严谨、合规、可用于课程作业的金融分析与参考建议。

                严格遵守以下全部规则：
                1.
                信息来源唯一：所有分析、数据、结论必须100 % 来自文档原文和历史数据，禁止编造、引用外部信息、预测股价。
                2.
                分析范围：覆盖财务表现、业务结构、成长性、盈利能力、偿债能力、经营风险、行业地位、募资用途（招股书）。
                3.
                合规底线：绝对不出现“买入、卖出、持有、加仓、减仓、推荐、翻倍、稳赚”等投资指令，只做客观分析与参考建议。
                4.
                无信息处理：文档无相关内容时，仅回复：“根据现有文档，无法提供相关分析建议。”
                5.
                若用户问及某公司的整体经营状况或投资建议等，必须以以下固定格式输出（必须严格执行）：
                【核心财务概况】
                【经营与成长性分析】
                【盈利能力与偿债能力】
                【核心风险提示】
                【金融参考建议（非投资建议）】
                6.
                结尾必须加风险提示：
                【重要声明】本内容仅基于公开文档分析，不构成任何投资建议。投资有风险，决策需谨慎。
                '''
            else:
                chat_entry.system_prompt = "你是一个智能助手"
            chat_entry.save()
            # chat_entry.keywords.refresh_from_db()
            messages.success(request, '对话创建成功！')
            return redirect('chat:chat_entry_info', chat_id=chat_entry.id)
    else:
        form = ChatEntryForm(user=request.user)
    return render(request, 'chat/chat_entry_form.html', {'form': form, 'title': '创建对话'})

@login_required
def chat_entry_update(request, pk):
    chat_entry = get_object_or_404(ChatEntry, pk=pk, user=request.user)
    if request.method == 'POST':
        form = ChatEntryForm(request.POST, user=request.user, instance=chat_entry)
        if form.is_valid():
            form.save()
            messages.success(request, '对话更新成功！')
            return redirect('chat:chat_entry_info', chat_id=chat_entry.id)
    else:
        form = ChatEntryForm(user=request.user, instance=chat_entry)
    return render(request, 'chat/chat_entry_form.html', {'form': form, 'title': '编辑对话'})

@login_required
def chat_entry_delete(request, pk):
    chat_entry = get_object_or_404(ChatEntry, pk=pk, user=request.user)
    chat_entry.delete()
    messages.success(request, '对话删除成功！')
    return redirect('chat:chat_entry_list')

# ==============================================
# 7. 隐私对话验证、对话详情、用户资料、流式对话（无修改，保留原有）
# ==============================================
@login_required
def private_chat_verify(request, chat_id):
    chat_entry = get_object_or_404(ChatEntry, id=chat_id, user=request.user, is_private=True)
    profile = get_object_or_404(UserProfile, user=request.user)
    if request.method == 'POST':
        form = PrivacyPasswordVerifyForm(request.POST)
        if form.is_valid():
            import hashlib
            pwd = form.cleaned_data['privacy_password']
            pwd_hash = hashlib.sha256(pwd.encode('utf-8')).hexdigest()
            if pwd_hash == profile.privacy_password_hash:
                request.session[f'private_chat_{chat_id}'] = True
                return redirect('chat:chat_detail', chat_id=chat_id)
            else:
                form.add_error('privacy_password', '密码错误，请重试！')
    else:
        form = PrivacyPasswordVerifyForm()
    return render(request, 'chat/private_verify.html', {'form': form, 'chat_entry': chat_entry})

@login_required
def chat_detail(request, chat_id):
    chat_entry = get_object_or_404(ChatEntry, id=chat_id, user=request.user)


    # ✅ 核心限制：仅允许从 chat_entry_info 跳转进入，禁止直接输URL访问
    if not request.session.get(f'from_info_{chat_id}', False):
        messages.error(request, "禁止直接访问！请从对话详情页进入")
        return redirect('chat:chat_entry_info', chat_id=chat_id)

    # 隐私对话二次校验（兜底）
    #if chat_entry.is_private and not request.session.get(f'private_chat_verified_{chat_id}', False):
    #    return redirect('chat:chat_verify_privacy', chat_id=chat_id)



    #if chat_entry.is_private:
        #if not request.session.get(f'private_chat_verified_{chat_id}', False):
            #return redirect('chat:chat_verify_privacy', chat_id=chat_id)



    #chat_messages = chat_entry.messages.all().order_by('created_at')
    chat_messages = ChatMessage.objects.filter(
        chat_entry=chat_entry
    ).order_by('created_at')
    return render(request, 'chat/chat_detail.html', {
        'chat_entry': chat_entry,
        'chat_messages': chat_messages,
        'page_title': f"对话 - {chat_entry.title}",
    })

@login_required
def entries_by_keyword(request, slug):
    """按关键字筛选当前用户的对话"""
    entries = ChatEntry.objects.filter(
        user=request.user,
        keywords__keyword__slug=slug  # 官方推荐的关键字查询方式
    )
    return render(request, 'chat/chat_entry_list.html', {
        'entries': entries,
        'current_keyword': slug
    })

'''
@login_required
def profile_edit(request):
    profile, created = UserProfile.objects.get_or_create(user=request.user)
    if request.method == 'POST':
        form = UserProfileForm(request.POST, instance=profile)
        if form.is_valid():
            form.save()
            messages.success(request, '资料更新成功！')
            return redirect('category_list')
    else:
        form = UserProfileForm(instance=profile)
    return render(request, 'chat/profile_edit.html', {'form': form, 'title': '个人资料'})

'''
