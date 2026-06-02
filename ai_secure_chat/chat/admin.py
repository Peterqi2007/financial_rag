from django.contrib import admin
from .models import (
    UserProfile,
    Category,
    Folder,
    ChatEntry,
    ChatMessage,
    ModelConfig
)

# ==============================================
# 对话消息内联（嵌入对话条目，核心功能保留）
# ==============================================
class ChatMessageInline(admin.TabularInline):
    model = ChatMessage
    extra = 0
    readonly_fields = ("created_at",)
    fields = ("role", "content", "is_stream", "created_at")

# ==============================================
# 1. 用户资料管理（单独注册，不修改原生User）
# ==============================================
@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "default_model", "api_key", "created_at")
    search_fields = ("user__username",)
    readonly_fields = ("created_at", "updated_at")

# ==============================================
# 2. 分类管理
# ==============================================
@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "order", "created_at")
    search_fields = ("name",)
    list_filter = ("user",)
    ordering = ("order", "-created_at")

# ==============================================
# 3. 文件夹管理
# ==============================================
@admin.register(Folder)
class FolderAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "category", "parent_folder", "order", "created_at")
    search_fields = ("name",)
    list_filter = ("user", "category")
    ordering = ("order", "-created_at")

# ==============================================
# 4. 对话条目管理（核心）
# ==============================================
@admin.register(ChatEntry)
class ChatEntryAdmin(admin.ModelAdmin):
    inlines = (ChatMessageInline,)
    list_display = ("title", "user", "folder", "is_private", "temperature", "created_at", "updated_at")
    search_fields = ("title", "system_prompt")
    list_filter = ("user", "folder", "is_private")
    readonly_fields = ("created_at", "updated_at")
    ordering = ("-updated_at",)

# ==============================================
# 5. 模型参数配置管理
# ==============================================
@admin.register(ModelConfig)
class ModelConfigAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "model_name", "temperature", "is_global")
    search_fields = ("name", "model_name")
    list_filter = ("user", "is_global")
    ordering = ("-id",)