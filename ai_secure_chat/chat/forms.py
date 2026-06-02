from django import forms
from django.contrib.auth import get_user_model
from .models import (
    Category, Folder, ChatEntry, UserProfile, ModelConfig
)
# 导入 Mezzanine 原生的 ProfileForm
from mezzanine.accounts.forms import ProfileForm as MezzanineProfileForm
from mezzanine.accounts import ProfileNotConfigured
from mezzanine.conf import settings
import bcrypt
import hashlib


# ==============================================
# 1. 分类(Category)表单
# 用于分类的新增/编辑
# ==============================================
class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ['name', 'order']
        labels = {
            'name': Category._meta.get_field('name').verbose_name,
            'order': Category._meta.get_field('order').verbose_name,
        }
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入分类名称'}),
            'order': forms.NumberInput(attrs={'class': 'form-control', 'min': 0}),
        }


# ==============================================
# 2. 文件夹(Folder)表单
# 用于文件夹的新增/编辑，支持分类和父文件夹关联
# ==============================================
class FolderForm(forms.ModelForm):
    class Meta:
        model = Folder
        fields = ['name', 'order', 'category', 'parent_folder']
        labels = {
            'name': Folder._meta.get_field('name').verbose_name,
            'order': Folder._meta.get_field('order').verbose_name,
            'category': Folder._meta.get_field('category').verbose_name,
            'parent_folder': Folder._meta.get_field('parent_folder').verbose_name,
        }
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入文件夹名称'}),
            'order': forms.NumberInput(attrs={'class': 'form-control', 'min': 0}),  """奇怪的order输入"""
            'category': forms.Select(attrs={'class': 'form-select'}),
            'parent_folder': forms.Select(attrs={'class': 'form-select'}),
        }

    def __init__(self, *args, **kwargs):
        # 初始化时过滤当前用户的分类和文件夹（需在视图中传入user参数）
        self.user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)

        if self.user:
            # 分类仅显示当前用户的
            self.fields['category'].queryset = Category.objects.filter(user=self.user)
            # 父文件夹仅显示当前用户的
            self.fields['parent_folder'].queryset = Folder.objects.filter(user=self.user)
            # 空值处理
            self.fields['category'].empty_label = "无分类"
            self.fields['parent_folder'].empty_label = "无父文件夹"


# ==============================================
# 3. 对话条目(ChatEntry)表单
# 用于对话条目的新增/编辑，包含模型参数配置
# ==============================================
class ChatEntryForm(forms.ModelForm):
    class Meta:
        model = ChatEntry
        fields = ['title', 'description', 'temperature', 'top_p', 'max_tokens', 'is_private', 'folder', 'keywords']
        labels = {
            'title': ChatEntry._meta.get_field('title').verbose_name,
            'description': ChatEntry._meta.get_field('description').verbose_name,
            'temperature': ChatEntry._meta.get_field('temperature').verbose_name,
            'top_p': ChatEntry._meta.get_field('top_p').verbose_name,
            'max_tokens': ChatEntry._meta.get_field('max_tokens').verbose_name,
            'is_private': ChatEntry._meta.get_field('is_private').verbose_name,
            'folder': ChatEntry._meta.get_field('folder').verbose_name,
            'keywords': ChatEntry._meta.get_field('keywords').verbose_name,
        }
        widgets = {
            'title': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入对话标题'}),
            'description': forms.Textarea(attrs={'class': 'form-control','rows': 3,'placeholder': '输入对话描述（可选）'}),
            'temperature': forms.NumberInput(attrs={'class': 'form-control', 'min': 0.0, 'max': 1.0, 'step': 0.01}),
            'top_p': forms.NumberInput(attrs={'class': 'form-control', 'min': 0.0, 'max': 1.0, 'step': 0.01}),
            'max_tokens': forms.NumberInput(attrs={'class': 'form-control', 'min': 1, 'max': 8192}),
            'is_private': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'folder': forms.Select(attrs={'class': 'form-select'}),
            # ✅ 关键字输入框：逗号分隔多个关键字，适配Bootstrap样式
            'keywords': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '多个关键字用英文逗号分隔（如：AI,对话,安全）'}),
        }


    def __init__(self, *args, **kwargs):
        # 初始化时过滤当前用户的文件夹
        self.user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)

        if self.user:
            self.fields['folder'].queryset = Folder.objects.filter(user=self.user)
            self.fields['folder'].empty_label = "请选择所属文件夹"


# ==============================================
# 4. 隐私对话密码验证表单
# 用于访问隐私对话时验证密码（明文输入，后端验证哈希）
# ==============================================
class PrivacyPasswordVerifyForm(forms.Form):
    privacy_password = forms.CharField(
        label="隐私密码",
        widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': '请输入隐私对话密码'}),
        min_length=6,
        error_messages={
            'required': '请输入隐私密码',
            'min_length': '密码长度不能少于6位'
        }
    )

# ==============================================
# 5. 用户资料(UserProfile)表单
# 用于管理用户扩展资料，包含隐私密码设置（自动哈希存储）
# ==============================================

User = get_user_model()


class UserProfileForm(MezzanineProfileForm):
    """
    终极兼容版：
    1. 保留原生【登录密码】修改功能（password1/password2）
    2. 新增独立【隐私对话密码】功能（privacy_password/privacy_password_confirm）
    3. 无重复字段，无BUG，不影响登录
    """
    # ====================== 独立隐私密码字段（和原生登录密码完全区分） ======================
    privacy_password = forms.CharField(
        label="隐私对话密码",
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': '设置隐私对话密码（仅用于解锁隐私对话）'
        }),
        required=False,
        min_length=6
    )
    privacy_password_confirm = forms.CharField(
        label="确认隐私对话密码",
        widget=forms.PasswordInput(attrs={'class': 'form-control'}),
        required=False
    )

    class Meta:
        model = User
        # 保留原生所有字段：包含登录密码修改字段
        fields = ("username", "email", "first_name", "last_name")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # 加载用户私有配置
        self.profile = None
        if self.instance and self.instance.pk:
            self.profile, _ = UserProfile.objects.get_or_create(user=self.instance)

        # 添加你的业务字段
        self.fields["default_model"] = forms.ChoiceField(
            label="默认大模型",
            choices=[('qwen-plus', '通义千问Plus'), ('gpt-3.5-turbo', 'GPT-3.5'), ('gpt-4', 'GPT-4')],
            initial=self.profile.default_model if self.profile else "qwen-plus",
            widget=forms.Select(attrs={'class': 'form-select'})
        )
        self.fields["api_key"] = forms.CharField(
            label="API密钥",
            initial=self.profile.api_key if self.profile else "",
            widget=forms.TextInput(attrs={'class': 'form-control'})
        )

    # 仅验证隐私密码，不干扰原生登录密码
    def clean(self):
        cleaned_data = super().clean()
        # 只校验你的隐私密码，原生密码由Mezzanine自动校验
        pwd = cleaned_data.get("privacy_password")
        pwd2 = cleaned_data.get("privacy_password_confirm")
        if pwd and pwd != pwd2:
            self.add_error("privacy_password_confirm", "两次输入的隐私密码不一致")
        return cleaned_data

    # 同时保存：原生User + 你的UserProfile
    def save(self, commit=True):
        # 保存原生用户数据（含登录密码）
        user = super().save(commit=commit)

        # 保存你的隐私配置
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.default_model = self.cleaned_data.get("default_model")
        profile.api_key = self.cleaned_data.get("api_key", "")

        # 加密保存隐私对话密码
        pwd = self.cleaned_data.get("privacy_password")
        if pwd:
                profile.privacy_password_hash = bcrypt.hashpw(pwd.encode(), bcrypt.gensalt()).decode() # 哈希加密
        if commit:
            profile.save()
        return user

# ==============================================
# 6. 模型参数配置(ModelConfig)表单
# 用于管理全局/自定义模型参数模板
# ==============================================

class ModelConfigForm(forms.ModelForm):
    class Meta:
        model = ModelConfig
        fields = ['name', 'model_name', 'temperature', 'top_p', 'max_tokens', 'is_global']
        labels = {
            'name': ModelConfig._meta.get_field('name').verbose_name,
            'model_name': ModelConfig._meta.get_field('model_name').verbose_name,
            'temperature': ModelConfig._meta.get_field('temperature').verbose_name,
            'top_p': ModelConfig._meta.get_field('top_p').verbose_name,
            'max_tokens': ModelConfig._meta.get_field('max_tokens').verbose_name,
            'is_global': ModelConfig._meta.get_field('is_global').verbose_name,
        }
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '请输入配置名称'}),
            'model_name': forms.Select(attrs={'class': 'form-select'},
                                       choices=[('qwen-plus', '通义千问Plus'), ('gpt-3.5-turbo', 'GPT-3.5'),
                                                ('gpt-4', 'GPT-4')]),
            'temperature': forms.NumberInput(attrs={'class': 'form-control', 'min': 0.0, 'max': 1.0, 'step': 0.01}),
            'top_p': forms.NumberInput(attrs={'class': 'form-control', 'min': 0.0, 'max': 1.0, 'step': 0.01}),
            'max_tokens': forms.NumberInput(attrs={'class': 'form-control', 'min': 1, 'max': 8192}),
            'is_global': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }
