"""
URL configuration for omnic app.
"""
from django.urls import path

from . import views
urlpatterns = [
    path('', views.home, name='get-home'),
    path('collect-sample', views.collect_sample, name='post-collect-sample'),
]
