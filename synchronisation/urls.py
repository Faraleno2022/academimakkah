from django.urls import path

from . import views
from .media_views import media_file


app_name = 'synchronisation'

urlpatterns = [
    path('health/', views.health, name='health'),
    path('revision/', views.revision, name='revision'),
    path('devices/setup/', views.device_setup, name='device_setup'),
    path('devices/register/', views.register_device, name='register_device'),
    path('devices/liaison/', views.liaison, name='liaison'),
    path('push/', views.push, name='push'),
    path('pull/', views.pull, name='pull'),
    path('media/', media_file, name='media'),
]
