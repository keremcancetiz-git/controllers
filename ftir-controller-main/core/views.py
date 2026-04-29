from django.http import HttpResponse
from rest_framework.decorators import api_view
from rest_framework.response import Response


@api_view(['GET'])
def home(request):
    return Response({'function': 'health', 'message': 'Gooood morning vietnam!!'}, status=200)
