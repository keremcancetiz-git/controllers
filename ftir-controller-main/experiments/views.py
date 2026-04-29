from django.shortcuts import render
import os
import sys
import time
import traceback
from datetime import datetime
import shutil
import zipfile

from django.shortcuts import render
from rest_framework.decorators import api_view
from rest_framework.response import Response

from django.http import FileResponse

from experiments.utils import pick_absorbance

ROOT_FOLDER = os.path.expanduser("~\\Desktop\\FTIR")
# ROOT_FOLDER = "C:\\Users\\Innof\\Desktop\\FTIR"
# ROOT_FOLDER = "C:\\Users\\innoflex\\Desktop\\FTIR"
# ROOT_FOLDER = "C:\\Users\\Administrator\\OneDrive - InnoFlex B.V\\Desktop\\FTIR"


@api_view(['GET'])
def home(request):
    return Response("OMNIC!!")


@api_view(['GET'])
def experiment_folders(request):
    try:
        folders = []
        experiments_folder = os.path.join(ROOT_FOLDER, "Experiments")

        for root, dirs, _ in os.walk(experiments_folder):
            for dir_name in dirs:
                full_path = os.path.join(root, dir_name)
                created_at = time.ctime(os.path.getctime(full_path))
                relative_path = os.path.relpath(full_path, experiments_folder)
                folders.append((relative_path, created_at))

        return Response(folders, status=200)
    except Exception as e:
        print(e)
        traceback.print_exc()
        return Response({'error': f'Error {e}'}, status=500)

@api_view(['POST'])
def get_absorbance(request):
    """
    Returns absorbance for all spectra in experiment folder.
    """
    data = request.data
    experiment_name = data.get('experiment_name', None)
    wavenumber = data.get('wavenumber', None)
    experiment_folder = os.path.join(ROOT_FOLDER, "Experiments", experiment_name)
    absorbance_results = []
    
    try:
        csv_files = [f for f in os.listdir(experiment_folder) if f.endswith('.csv') and not f.startswith('bkg')]
        
        if csv_files:
            # Sort files by name (chronological order due to timestamp format)
            # csv_files.sort()
            
            for csv_file in csv_files:
                csv_path = os.path.join(experiment_folder, csv_file)
                
                # Extract timestamp from filename (remove .csv extension)
                timestamp_str = csv_file.replace('.csv', '')
                
                # Convert timestamp string to milliseconds since epoch
                try:
                    # Parse the timestamp format: 2026_01_07-10_57_48
                    dt = datetime.strptime(timestamp_str, '%Y_%m_%d-%H_%M_%S')
                    timestamp_ms = int(dt.timestamp() * 1000)
                except ValueError as e:
                    print(f"Error parsing timestamp from {csv_file}: {e}")
                    timestamp_ms = None
                
                # Process the spectrum for this file
                absorbance = pick_absorbance(csv_path, wavenumber)
                
                # Add result with timestamp in milliseconds
                absorbance_results.append({
                    'timestamp': timestamp_ms,
                    'absorbance': absorbance
                })
                
            print(f"Processed {len(csv_files)} CSV files")
        else:
            print("No CSV files found in folder")
            
    except Exception as e:
        print(f"Error processing CSV files: {e}")
    
    return Response({
        'function': 'getAbsorbance',
        'wavenumber': wavenumber,
        'results': absorbance_results
    }, status=200)