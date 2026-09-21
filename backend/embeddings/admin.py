from django.contrib import admin

from .models import EmbeddingChunk, KnowledgeDocument


@admin.register(KnowledgeDocument)
class KnowledgeDocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "source_type", "company_id", "created_at")
    list_filter = ("source_type",)
    search_fields = ("title", "content", "source_url")
    readonly_fields = ("created_at",)


@admin.register(EmbeddingChunk)
class EmbeddingChunkAdmin(admin.ModelAdmin):
    list_display = ("document", "chunk_index", "model", "created_at")
    list_filter = ("model",)
    readonly_fields = ("created_at",)
