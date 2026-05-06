# OMNIC DDE wrapper

import sys
from enum import Enum
from typing import Any
 
 
class Command(Enum):
    Delay = "Delay"
    Export = "Export"
    CollectSample = "Invoke CollectSample Auto Polling"
    CollectBackground = "Invoke CollectBackground Auto Polling"
    DisplayLimits = "DisplayLimits"
    Display = "Display"
    DeleteSpectrum = "DeleteSpectrum"
    Select = "Select"
 
    def __str__(self):
        return self.value
 
 
class Result(Enum):
    Current = "Current"
 
    def __str__(self):
        return self.value
 
 
class Conversation:
    def __init__(self):
        self.conversation = None
        self.client = None
        if sys.platform == 'win32':
            self._init_windows_conversation()
 
    def _init_windows_conversation(self):
        import win32ui
        import dde  # part of pywin32
        self.client = dde.CreateServer()
        self.client.Create("OMNIC")
        self.conversation = dde.CreateConversation(self.client)
        self.conversation.ConnectTo("OMNIC", "SPECTRA")
 
    def exec(self, commands) -> None:
        """Build a DDE command string from a list of dicts and send it."""
        command_str = "["
        for command_object in commands:
            if command_object['command'] not in Command:
                raise ValueError(f"Invalid command: {command_object['command']}")
            if command_object.get('args', None):
                command_str += str(command_object['command']) + ' ' + command_object['args']
            else:
                command_str += str(command_object['command'])
            if len(commands) > 1 and command_object != commands[-1]:
                command_str += ";"
        command_str += "]"
 
        if self.conversation:
            print("CMD: " + command_str)
            self.conversation.Exec(command_str)
        else:
            print(f"[stub] CMD: {command_str}")
 
    def request(self, parameter: str) -> Any:
        """
        Request any DDE parameter.
 
        Examples:
            conv.request('MenuStatus CollectSample')  -> 'Enabled' | 'Disabled'
            conv.request('Collect Status')            -> comma-separated string
            conv.request('Collect NumScans')          -> '10'
        """
        if self.conversation:
            return self.conversation.Request(parameter)
        return 'Enabled'  # stub so polling loops terminate off-Windows
 
    def req_result(self, target_result) -> Any:
        """Request a Result group parameter (e.g. Result.Current -> 'Result Current')."""
        return self.request("Result " + str(target_result))
 
    def destroy(self):
        if self.client:
            self.client.Destroy()
            self.client = None
            self.conversation = None