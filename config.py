
import os

class Config:

    BOT_TOKEN = "7260809129:AAHdC33qW3dUMkyseDjmB-KRE5MCFifU_cA"
    APP_ID = 27999679
    API_HASH = "f553398ca957b9c92bcb672b05557038"

    #comma seperated user id of users who are allowed to use
    ALLOWED_USERS = [x.strip(' ') for x in os.environ.get('ALLOWED_USERS','1423807625,1048110820,6520490787,7100701721,7297547385').split(',')]
 # Absolute path to the folder where you keep your .ttf/.otf files
    FONTS_DIR = os.path.join(os.getcwd(), "fonts")
    
    DOWNLOAD_DIR = 'downloads'
