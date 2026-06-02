from django.urls import path
from . import views

app_name = 'chat'  # 命名空间，与模板中{% url 'chat:xxx' %}对应

urlpatterns = [
    # ===================== 流式对话接口 =====================
    path('chat/<int:chat_id>/stream/', views.chat_stream, name='chat_stream'),
    # 备用：一次性发送消息的接口
    path('<int:chat_id>/send/', views.chat_send, name='chat_send'),

    # ===================== 分类管理 =====================
    path('categories/', views.category_list, name='category_list'),
    path('category/create/', views.category_create, name='category_create'),
    path('category/<int:pk>/update/', views.category_update, name='category_update'),
    path('category/<int:pk>/delete/', views.category_delete, name='category_delete'),

    # ===================== 文件夹管理 =====================
    path('folders/', views.folder_list, name='folder_list'),
    path('folders/category/<int:category_id>/', views.folder_list, name='folder_list'),  # 按分类筛选文件夹
    # 子文件夹详情（必传分类ID+文件夹ID）
    path('folders/category/<int:category_id>/folder/<int:folder_id>/', views.folder_detail, name='folder_detail'),
    path('folder/create/', views.folder_create, name='folder_create'),
    path('folder/<int:pk>/update/', views.folder_update, name='folder_update'),
    path('folder/<int:pk>/delete/', views.folder_delete, name='folder_delete'),

    # ===================== 对话条目管理 =====================
    path('chat-entries/', views.chat_entry_list, name='chat_entry_list'),
    path('chat-entries/folder/<int:folder_id>/', views.chat_entry_list, name='chat_entry_list'),  # 按文件夹筛选对话
    path('chat/create/', views.chat_entry_create, name='chat_entry_create'),
    path('chat/<int:pk>/update/', views.chat_entry_update, name='chat_entry_update'),
    path('chat/<int:pk>/delete/', views.chat_entry_delete, name='chat_entry_delete'),

    # ===================== 隐私验证 + 对话详情 =====================
    path('chat/<int:chat_id>/verify-privacy/', views.chat_verify_privacy, name='chat_verify_privacy'),
    path('chat/<int:chat_id>/info/', views.chat_entry_info, name='chat_entry_info'),  # 对话信息页（触发隐私验证）
    path('chat/<int:chat_id>/private-verify/', views.private_chat_verify, name='private_chat_verify'),  # 旧版隐私验证（兜底）
    path('chat/<int:chat_id>/detail/', views.chat_detail, name='chat_detail'),  # 对话详情页（需从info页跳转）

    # ===================== 按关键词筛选对话 =====================
    path('entries/keyword/<slug:slug>/', views.entries_by_keyword, name='entries_by_keyword'),
]
