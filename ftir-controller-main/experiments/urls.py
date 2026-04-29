"""
URL configuration for omnic app.
"""
from django.urls import path

from . import views

urlpatterns = [
    path('', views.home, name='get-home'),
    path('experiment-folders', views.experiment_folders, name='get-experiment-folders'),
    path('get-absorbance', views.get_absorbance, name='get-absorbance'),
]
