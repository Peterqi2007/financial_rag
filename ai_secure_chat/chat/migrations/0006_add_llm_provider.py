# Generated manually for adding llm_provider field to UserProfile
# Migration 0006: add_llm_provider

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0005_chatentry_description"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="llm_provider",
            field=models.CharField(
                blank=True,
                choices=[
                    ("qwen", "通义千问"),
                    ("deepseek", "DeepSeek"),
                    ("openai", "OpenAI"),
                ],
                default="qwen",
                max_length=30,
                verbose_name="默认 LLM 厂商",
            ),
        ),
    ]
