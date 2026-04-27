ftir-controller
======================

## Table of Contents

1. [Introduction](#introduction)  
2. [Setup Instructions](#setup-instructions)  
3. [Update Instructions](#updating-instructions)
4. [Other](#other)

### Introduction

The FTIR-controller-MQTT is an MQTT based backend for controlling the OMNIC software running on the host computer. By calling the commands remotely via MQTT, the python script will execute the corresponding commands on OMNIC and thus, the FTIR. The implementation of OMNIC commands that control the OMNIC software via DDE are based on their documentation in this [document](https://mmrc.caltech.edu/FTIR/Nicolet/OMNIC%20DDE%20Commands%20&%20Parameters.pdf), which is also available in the InnoFlex OneDrive. Note, not all of them work on our FTIR device.


### Setup Instructions

1. Clone the repository into a folder by using the git command line interface or the VS Code git plugin. If the code already exists on the PC, make sure to check for updates by fetching the latest commits from the github repository.

2. Update/Install the required dependencies by running `pip install -r requirements.txt`.

3. Run the controller by executing the startup script or directly with `python main.py`. Keep the terminal window open — closing it will stop the process.

4. Make sure OMNIC is open when the server is started.

## Run
 
```bat
start.bat
```
 
Leaves a window open with logs; closes on Ctrl-C.
 
## Topics
 
| Direction | Topic                                | Payload                                               |
|-----------|--------------------------------------|-------------------------------------------------------|
| in        | `master/bypass/FTIR/collect-bkg`                   | `{"experiment_name": "<name>", "experiment_name_suffix": "<suffix>"}` (`experiment_name_suffix` optional — appended to the exported filename) |
| out       | `master/bypass/FTIR/collect-bkg/response`          | `{"status": "success"\|"error"\|"busy", ...}`         |
| in        | `master/bypass/FTIR/collect-sample`                | `{"experiment_name": "<name>", "experiment_name_suffix": "<suffix>"}` (`experiment_name_suffix` optional — appended to the exported filename) |
| out       | `master/bypass/FTIR/collect-sample/response`       | `{"status": "success"\|"error"\|"busy", ...}`         |
| in        | `master/bypass/FTIR/experiment-folders`            | `{}` (empty)                                          |
| out       | `master/bypass/FTIR/experiment-folders/response`   | `{"status": "success", "folders": [[name, ctime]]}`   |
| in        | `master/bypass/FTIR/get-absorbance`                | `{"experiment_name": "...", "wavenumber": 1234.5}`    |
| out       | `master/bypass/FTIR/get-absorbance/response`       | `{"status": "success", "results": [{ts, absorbance}]}`|
 
## Busy semantics
 
`collect-bkg` and `collect-sample` run in worker threads. A global busy
flag rejects overlapping requests:
 
```json
{"status": "busy", "function": "collectSample"}
```
 
`experiment-folders` and `get-absorbance` run synchronously and ignore the
busy flag — they only touch the filesystem, not OMNIC.
 
## Files
 
- `main.py` — MQTT client, message handlers, worker threads
- `omnic.py` — DDE `Conversation` wrapper and `Command` enum
- `config.json` — broker + path settings
- `Requirements.txt` — pinned dependencies
- `start.bat` — double-click launcher
