from django.db import models
from django.contrib.auth.models import User
from django.core.validators import MinValueValidator, MaxValueValidator
from django.conf import settings
from django.utils import timezone
# Mezzanine 关键字字段（官方标准）
from mezzanine.generic.fields import KeywordsField
from cryptography.fernet import Fernet
import hashlib
import bcrypt
import base64


# ====================== 加密工具（固定不变，基于项目SECRET_KEY）======================
def get_fernet_cipher():
    """生成唯一加密器，用项目SECRET_KEY作为根密钥"""
    # 把Django的SECRET_KEY处理成Fernet要求的32位密钥
    key_material = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    fernet_key = base64.urlsafe_b64encode(key_material)
    return Fernet(fernet_key)

# ==============================================
# 1. 用户扩展资料模型（核心：隐私密码、LLM偏好、RAG配置）
# ==============================================
class UserProfile(models.Model):
    # 一对一关联系统用户，用户删除则资料同步删除
    user = models.OneToOneField(User, on_delete=models.CASCADE, unique=True, verbose_name="所属用户")
    # 隐私对话密码（哈希存储，绝不存明文！）
    privacy_password_hash = models.CharField(max_length=256, blank=True, default='', verbose_name="隐私密码")
    # 默认使用的 LLM 厂商（qwen / deepseek / openai 等）
    llm_provider = models.CharField(
        max_length=30,
        default="qwen",
        blank=True,
        choices=[
            ("qwen", "通义千问"),
            ("deepseek", "DeepSeek"),
            ("openai", "OpenAI"),
        ],
        verbose_name="默认 LLM 厂商",
    )
    # 默认使用的大模型名称
    default_model = models.CharField(max_length=50, default="qwen-plus", blank=True, verbose_name="默认大模型")

    # ====================== API密钥加密存储 ======================
    _api_key_encrypted = models.CharField(max_length=512, default='', verbose_name="API密钥密文")

    @property
    def api_key(self):
        """读取时自动解密"""
        if not self._api_key_encrypted:
            return ""
        try:
            return get_fernet_cipher().decrypt(self._api_key_encrypted.encode()).decode()
        except Exception:
            return ""

    @api_key.setter
    def api_key(self, raw_value):
        """写入时自动加密"""
        if not raw_value:
            self._api_key_encrypted = ""
            return
        self._api_key_encrypted = get_fernet_cipher().encrypt(raw_value.encode()).decode()

    # 创建/更新时间
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    # ====================== RAG 外部知识库检索增强配置（Coze）======================
    # 是否启用 RAG 知识库检索
    rag_enabled = models.BooleanField(default=False, verbose_name="启用 RAG 知识库")
    # Coze 知识库 API 地址
    rag_base_url = models.CharField(
        max_length=512,
        default="https://5z3ysb9pn9.coze.site/run",
        blank=True,
        verbose_name="知识库 API 地址",
    )
    # Coze API Token（加密存储，与 api_key 共用 Fernet 方案）
    _rag_api_token_encrypted = models.CharField(
        max_length=512, default="", blank=True, verbose_name="知识库 Token 密文"
    )
    # Coze 数据集名称
    rag_dataset_name = models.CharField(
        max_length=100, default="knowledge_base", blank=True, verbose_name="知识库数据集名称"
    )
    # 检索返回条数（1-20）
    rag_top_k = models.IntegerField(
        default=4,
        validators=[MinValueValidator(1), MaxValueValidator(20)],
        verbose_name="检索返回条数",
    )
    # 最低相似度阈值（0-1），低于此分数的文档将被丢弃
    rag_min_score = models.FloatField(
        default=0.5,
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
        verbose_name="最低相似度阈值",
    )

    # ====================== RAG Token 自动加解密属性 ======================
    @property
    def rag_api_token(self):
        """读取 RAG Token 时自动解密"""
        if not self._rag_api_token_encrypted:
            return ""
        try:
            return get_fernet_cipher().decrypt(self._rag_api_token_encrypted.encode()).decode()
        except Exception:
            return ""

    @rag_api_token.setter
    def rag_api_token(self, raw_value):
        """写入 RAG Token 时自动加密"""
        if not raw_value:
            self._rag_api_token_encrypted = ""
            return
        self._rag_api_token_encrypted = get_fernet_cipher().encrypt(raw_value.encode()).decode()

    # 密码验证方法
    def check_privacy_password(self, raw_password):
        return bcrypt.checkpw(raw_password.encode('utf-8'), self.privacy_password_hash.encode('utf-8'))

    class Meta:
        verbose_name = "用户资料"
        verbose_name_plural = "用户资料"

    def __str__(self):
        return f"{self.user.username} 的资料"

# ==============================================
# 2. 顶级分类模型（一级目录）
# ==============================================
class Category(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="categories", verbose_name="所属用户")
    name = models.CharField(max_length=100, verbose_name="分类名称")
    order = models.IntegerField(default=0, verbose_name="排序序号")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        verbose_name = "分类"
        verbose_name_plural = "分类"
        ordering = ["order", "created_at"]

    def __str__(self):
        return self.name

# ==============================================
# 3. 文件夹模型（支持无限嵌套）
# ==============================================
class Folder(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="folders", verbose_name="所属用户")
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="folders", blank=True, null=True, verbose_name="所属分类")
    parent_folder = models.ForeignKey("self", on_delete=models.CASCADE, related_name="child_folders", blank=True, null=True, verbose_name="父文件夹")
    name = models.CharField(max_length=100, verbose_name="文件夹名称")
    order = models.IntegerField(default=0, verbose_name="排序序号")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        verbose_name = "文件夹"
        verbose_name_plural = "文件夹"
        ordering = ["order", "created_at"]

    def __str__(self):
        return self.name

# ==============================================
# 4. 对话条目模型（核心业务：单个对话会话）
# ==============================================
class ChatEntry(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="chat_entries", verbose_name="所属用户")
    folder = models.ForeignKey(Folder, on_delete=models.CASCADE, related_name="chat_entries", verbose_name="所属文件夹")
    title = models.CharField(max_length=200, verbose_name="对话标题")
    description = models.CharField(max_length=255, blank=True, default="", verbose_name="对话简介")
    system_prompt = models.TextField(blank=True, default="你是一个智能助手", verbose_name="系统提示词")
    # 大模型调用参数
    temperature = models.FloatField(default=0.7, verbose_name="温度参数",
                                    validators=[MinValueValidator(0.0), MaxValueValidator(1.0)])
    top_p = models.FloatField(default=0.9, verbose_name="TopP参数",
                              validators=[MinValueValidator(0.0), MaxValueValidator(1.0)])
    max_tokens = models.IntegerField(default=2048, verbose_name="最大Token",
                                     validators=[MinValueValidator(1), MaxValueValidator(8192)])
    # 是否隐私对话
    is_private = models.BooleanField(default=False, verbose_name="是否隐私对话")
    # ✅ 是否对该对话启用 RAG 知识库检索增强
    use_rag = models.BooleanField(default=True, verbose_name="启用 RAG 检索增强")
    # Mezzanine 关键字/标签
    keywords = KeywordsField(verbose_name="对话关键字", blank=True)
    # 时间
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "对话条目"
        verbose_name_plural = "对话条目"
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title

# ==============================================
# 5. 对话消息模型（用户提问 + AI回复）
# ==============================================
class ChatMessage(models.Model):
    ROLE_CHOICES = (
        ("user", "用户"),
        ("assistant", "AI助手"),
    )
    chat_entry = models.ForeignKey(ChatEntry, on_delete=models.CASCADE, related_name="messages", verbose_name="所属对话")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, verbose_name="消息角色")
    content = models.TextField(verbose_name="消息内容")
    is_stream = models.BooleanField(default=True, verbose_name="是否流式输出")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="发送时间")

    class Meta:
        verbose_name = "对话消息"
        verbose_name_plural = "对话消息"
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.get_role_display()}：{self.content[:30]}..."

# ==============================================
# 6. 大模型参数配置模型
# ==============================================
class ModelConfig(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="model_configs", blank=True, null=True, verbose_name="所属用户")
    name = models.CharField(max_length=100, verbose_name="配置名称")
    model_name = models.CharField(max_length=50, default="qwen-plus", verbose_name="模型名称")
    temperature = models.FloatField(default=0.7, verbose_name="温度参数",
                                    validators=[MinValueValidator(0.0), MaxValueValidator(1.0)])
    top_p = models.FloatField(default=0.9, verbose_name="TopP参数",
                              validators=[MinValueValidator(0.0), MaxValueValidator(1.0)])
    max_tokens = models.IntegerField(default=2048, verbose_name="最大Token",
                                     validators=[MinValueValidator(1), MaxValueValidator(8192)])
    is_global = models.BooleanField(default=False, verbose_name="是否全局配置")

    class Meta:
        verbose_name = "模型参数配置"
        verbose_name_plural = "模型参数配置"

    def __str__(self):
        return self.name
