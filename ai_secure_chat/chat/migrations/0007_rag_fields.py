# Generated manually — adds RAG fields to UserProfile and use_rag to ChatEntry
# Migration 0007: rag_fields

from django.db import migrations, models
import django.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0006_add_llm_provider"),
    ]

    operations = [
        # ===== UserProfile RAG 配置字段 =====
        migrations.AddField(
            model_name="userprofile",
            name="rag_enabled",
            field=models.BooleanField(default=False, verbose_name="启用 RAG 知识库"),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="rag_base_url",
            field=models.CharField(
                blank=True,
                default="https://5z3ysb9pn9.coze.site/run",
                max_length=512,
                verbose_name="知识库 API 地址",
            ),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="_rag_api_token_encrypted",
            field=models.CharField(
                blank=True,
                default="",
                max_length=512,
                verbose_name="知识库 Token 密文",
            ),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="rag_dataset_name",
            field=models.CharField(
                blank=True,
                default="knowledge_base",
                max_length=100,
                verbose_name="知识库数据集名称",
            ),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="rag_top_k",
            field=models.IntegerField(
                default=4,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(20),
                ],
                verbose_name="检索返回条数",
            ),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="rag_min_score",
            field=models.FloatField(
                default=0.5,
                validators=[
                    django.core.validators.MinValueValidator(0.0),
                    django.core.validators.MaxValueValidator(1.0),
                ],
                verbose_name="最低相似度阈值",
            ),
        ),
        # ===== ChatEntry RAG 开关 =====
        migrations.AddField(
            model_name="chatentry",
            name="use_rag",
            field=models.BooleanField(default=True, verbose_name="启用 RAG 检索增强"),
        ),
    ]
