ftir-controller
======================

## Table of Contents

1. [Introduction](#introduction)  
2. [Setup Instructions](#setup-instructions)  
3. [Update Instructions](#updating-instructions)
4. [Other](#other)

### Introduction

The ftir-controller is a Django backend server used for controlling the OMNIC software running on the host computer. By calling the HTTP endpoints from a remote client, such as a raspberry pi, the Django server will execute commands on OMNIC and thus, the FTIR. This software uses Django with Rest API. More information about Django framework can be found [here](https://www.djangoproject.com/) and on the Django Rest framework [here](https://www.django-rest-framework.org/). The implementation of OMNIC commands that control the OMNIC software are based on their documentation in this [document](https://mmrc.caltech.edu/FTIR/Nicolet/OMNIC%20DDE%20Commands%20&%20Parameters.pdf), which is also available in the InnoFlex OneDrive. Note, not all of them work on our FTIR device.

The ftir-controller is a Django backend server used for controlling the OMNIC software running on the host computer. By calling the HTTP endpoints from a remote client, such as a raspberry pi, the Django server will execute commands on OMNIC and thus, the FTIR. This software uses Django with Rest API. More information about Django framewrok can be found [here](https://www.djangoproject.com/) and on the Django Rest framework [here](https://www.django-rest-framework.org/). The implementation of OMNIC commands that control the OMNIC software are based on their documentation in this [document](https://mmrc.caltech.edu/FTIR/Nicolet/OMNIC%20DDE%20Commands%20&%20Parameters.pdf), which is also available in the InnoFlex OneDrive. Note, not all of them work on our FTIR device.

When this version was being developed, a lot of functions for the OMNIC controller were implemented, which did not end up being used in the FTIR automatic sampler. Thus, in this repository, the `main` branch contains the simplified version of the FTIR controller, that is made more readable, and the branch `archive` contains the archived, older version of the FTIR controller, which has more functions implemented if needed for the future.

### Setup Instructions

1. Clone the repository into a folder by using the git command line interface or the VS Code git plugin. If the code already exists on the PC, make sure to check for updates by fetching the latest commits from the github repository.

2. Update/Install the required dependencies by running `pip install -r requirements.txt`.

3. Adjust ROOT_FOLDER in the code (multiple instances).

4. Run the server by running `python3 manage.py runserver 0.0.0.0:8080` or by executing the startup script. Make sure to keep the terminal window running in order not to kill the server process.

5. Make sure OMNIC is open when the server is started.

Given succesful setup, the other computers can access the HTTP endpoints of this server by making HTTP requests to `http://IP:8080`, for example `http://192.168.0.193:8080` (To get the exact IP of a Windows PC run `ipconfig` and assign a static IP to the computer to avoid future problems).

If some OMNIC command fails, such as sample collection, due to a faulty connection, unexpected interupt or something else, the first course of action is to restart the server and the OMNIC software. If the issue persists, then further investigation is needed.

Note: the system decimal seperator needs to be '.'.

### Updating instructions
The following section explains how to update this repository when changes have been made to the node-red flows. Ideally, it should be updated after every major change, a new feature or a bug fix.
1. Stage all changes with the VS Code git plugin.
2. Write a descriptive commit message and commit to local git repository.
3. Push the commits to a remote repository. NOTE: you might be requred to log in to a github account that has access to this repository (use personal access token instead of password). More details can be found in the earlier setup section of this manual or on this [page](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#creating-a-personal-access-token-classic).

