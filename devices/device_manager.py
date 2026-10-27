import json

def execute_command(command):
    """Simulate device interaction based on command"""
    with open('config/rules.json') as f:
        rules = json.load(f)

    # Map command to device
    device_map = {
        'turn_on_light': 'light_switch',
        'check_weather': 'internal_api_service',
        'access_database': 'internal_api_service'
    }

    device = device_map.get(command, 'unknown')

    return {
        'status': 'success',
        'device': device,
        'command': command
    }