import os
import sys
import time
import traceback
import subprocess
from datetime import datetime

from rest_framework.decorators import api_view
from rest_framework.response import Response

from omnic.omnic_instance import Conversation
from omnic.typez import Command, Result
# from omnic.utils import log_sample
from django.http import FileResponse

ROOT_FOLDER = os.path.expanduser("~\\Desktop\\FTIR")
# ROOT_FOLDER = "C:\\Users\\Innof\\Desktop\\FTIR"
#ROOT_FOLDER = "C:\\Users\\innoflex\\Desktop\\FTIR"
# ROOT_FOLDER = "C:\\Users\\Administrator\\OneDrive - InnoFlex B.V\\Desktop\\FTIR"

@api_view(['GET'])
def home(request):
    return Response("OMNIC!!")

@api_view(['POST'])
def collect_bkg(request):
    conversation = None

    try:
        data = request.data
        experiment_name = data.get('experiment_name', None)

        conversation = Conversation()

        conversation.exec([
            {
                'command': Command.CollectBackground,
            }
        ])
        

        menu_status = 'Disabled'
        while menu_status.strip() != 'Enabled':
            menu_status = conversation.conversation.Request('MenuStatus CollectBackground')
            time.sleep(1)

        # conversation.destroy()
        
        # conversation = Conversation()
        conversation.exec([
            {
                'command': Command.Display
            },
            {
                'command': Command.DisplayLimits,
                'args': "4000 500"
            }
        ])

        if experiment_name is not None:  
            
            timestamp_str = datetime.now().strftime('%Y_%m_%d-%H_%M_%S')
            file_name_csv = "bkg__" + timestamp_str + ".csv"
            file_name_spa = "bkg__" + timestamp_str + ".spa"

            experiment_folder = os.path.join(ROOT_FOLDER, "Experiments", experiment_name)
            os.makedirs(experiment_folder,exist_ok=True)

            print(experiment_name)
            export_file_csv = experiment_folder + "\\" + file_name_csv
            export_file_spa = experiment_folder + "\\" + file_name_spa

            conversation.exec([
                {
                    'command': Command.Export,
                    'args': export_file_spa
                }
            ])
            conversation.exec([
                {
                    'command': Command.Export,
                    'args': export_file_csv
                }
            ])
    
        conversation.destroy()
        return Response({'function': 'collectBackground', 'message': 'Background collection finished'}, status=200)
    except Exception as e:
        if conversation:
            conversation.destroy()
        print(e)
        traceback.print_exc()
        return Response({'function': 'collectBackground', 'error': f'Error {e}'}, status=500)



@api_view(['POST'])
def collect_sample(request):
    conversation = None
    try:
        data = request.data
        experiment_name = data.get('experiment_name', None)

        conversation = Conversation()

        conversation.exec([
            {
                'command': Command.CollectSample,
            }
        ])

        menu_status = 'Disabled'
        while menu_status.strip() != 'Enabled':
            menu_status = conversation.conversation.Request('MenuStatus CollectSample')
            time.sleep(1)


        #time.sleep(100)
        # conversation.destroy()
    
        # print(f"Sleep timer start at {datetime.now().strftime('%H:%M:%S')}")
        # if estimated_time is not None:
        #     time.sleep(int(estimated_time) + 10)
        # else:
        #     time.sleep(140)
        # print(f"Sleep timer end at {datetime.now().strftime('%H:%M:%S')}")
        # conversation = Conversation()
        conversation.exec([
            {
                'command': Command.Display
            },
            {
                'command': Command.DisplayLimits,
                'args': "4000 500"
            }
        ])
        
        if experiment_name is not None:

            timestamp_str = datetime.now().strftime('%Y_%m_%d-%H_%M_%S')
            file_name_csv = timestamp_str + ".csv"
            file_name_spa = timestamp_str + ".spa"

            experiment_folder = os.path.join(ROOT_FOLDER, "Experiments", experiment_name)
            os.makedirs(experiment_folder,exist_ok=True)
            
            export_file_csv = experiment_folder + "\\" + file_name_csv
            export_file_spa = experiment_folder + "\\" + file_name_spa

            conversation.exec([
                {
                    'command': Command.Export,
                    'args': export_file_spa
                }
            ])
            conversation.exec([
                {
                    'command': Command.Export,
                    'args': export_file_csv
                }
            ])
        
        conversation.destroy()
        return Response({'function': 'collectSample', 'message': 'Sample collection finished'}, status=200)
    except Exception as e:
        if conversation:
            conversation.destroy()
        print(e)
        traceback.print_exc()
        return Response({'function': 'collectSample', 'error': f'Error {e}'}, status=500)


