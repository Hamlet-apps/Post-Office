from django.urls import path

from . import editor, views

app_name = "content"

urlpatterns = [
    path("", views.templates, name="templates"),
    path("new/", views.template_new, name="template_new"),
    # Quick message dialog (loaded into #po-dialog-slot from any screen)
    path("quick-message/", views.quick_message, name="quick_message"),
    path("quick-message/search/", views.quick_message_search, name="quick_message_search"),
    path("quick-message/summary/", views.quick_message_summary, name="quick_message_summary"),
    # The editor's endpoints; owner is cmp_<id> or tpl_<id>
    path("_editor/<str:owner>/save/", editor.editor_save, name="editor_save"),
    path("_editor/<str:owner>/canvas/", editor.editor_canvas, name="editor_canvas"),
    path("_editor/<str:owner>/upload/", editor.editor_upload, name="editor_upload"),
    path("_editor/<str:owner>/image/", editor.editor_image, name="editor_image"),
    path("_editor/<str:owner>/copy/", editor.editor_copy, name="editor_copy"),
    # A template by its key (pavilion.order_confirmation). Keep these last.
    path("<str:key>/reset/", views.template_reset, name="template_reset"),
    path("<str:key>/delete/", views.template_delete, name="template_delete"),
    path("<str:key>/", editor.template_editor, name="template"),
]
