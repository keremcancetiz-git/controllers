import sys
from typing import Any

from omnic.typez import Command, Result


class Conversation:
    def __init__(self):
        self.conversation = None
        self.client = None
        if sys.platform == 'win32':
            self._init_windows_conversation()
        else:
            self._init_other_conversation()

    def _init_windows_conversation(self):
        try:
            import win32ui
            import win32con
            import win32api
            import dde

            # Initialize DDE client for Windows
            self.client = dde.CreateServer()  # Assign to self.client
            self.client.Create("OMNIC")
            self.conversation = dde.CreateConversation(self.client)
            self.conversation.ConnectTo("OMNIC", "SPECTRA")
        except ImportError:
            raise ImportError("win32 libraries are not available on this platform")

    def _init_other_conversation(self):
        # No DDE setup on non-Windows platforms
        self.conversation = None

    def exec(self, commands) -> None:
        """Executes the given command on Windows, or prints the command on other OS."""
        command_str = "["
        for command_object in commands:
            if command_object['command'] not in Command:
                raise ValueError(f"Invalid command: {command_object['command']}")

            if command_object.get('args', None):
                command_str += command_object['command'].__str__() + ' ' + command_object['args']
            else:
                command_str += command_object['command'].__str__()

            if len(commands) > 1 and command_object != commands[-1]:
                command_str += ";"
        command_str += "]"
        if self.conversation:
            # Execute command on Windows
            print("The COMMAND: " + command_str)
            self.conversation.Exec(command_str)
        else:
            # Print command on non-Windows platforms
            print(f"Executed command: {command_str}")

    def req(self, target_result) -> Any:
        """Requests the target result."""
        request_str = "Result " + target_result.__str__()
        if self.conversation:
            # Execute command on Windows
            result = self.conversation.Request(request_str)
        else:
            # Print command on non-Windows platforms
            print(f"Requested result: {request_str}")
            result = ''

        return result

    def destroy(self):
        """Clean up the DDE resources."""
        if self.client:
            self.client.Destroy()
