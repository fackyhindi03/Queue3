class Chat:

    START_TEXT = """👋 <b>Hlow there!</b>  
📌 <b>This is a Telegram Bot to Mux Subtitles into a Video.</b>  

🎬 <b>How to Use:</b>  
➡️ Send me a Telegram file, direct URL, or M3U8 link to begin!  
ℹ️ Type <code>/help</code> for more details.  

💡 <b>Credits:</b> @THe_vK_3
    """

    HELP_USER = "🤖 How can I assist you?"

    HELP_TEXT = """🆘 <b>Welcome to the Help Menu!</b>  

✅ <b>How to Use:</b>  
1️⃣ Send a video file, a direct URL, or an M3U8 stream link.  
2️⃣ Send a subtitle file (<code>.ass</code> or <code>.srt</code>).  
3️⃣ Choose your desired type of muxing!  

📌 <b>Custom File Name:</b>  
After choosing <code>/softmux</code>, <code>/hardmux</code>, or <code>/nosub</code>, the bot will ask you for a custom filename.

⚠️ <b>Note:</b>  
<i>Hardmux only supports English fonts. Other scripts may appear as empty blocks in the video!</i>  

🤖 <b>For Donghua watching, Visit:</b> <a href="https://fackyhindidonghuas.in/">Facky Hindi Donghua</a>  

💡 <b>Credits:</b> @THe_vK_3
    """

    NO_AUTH_USER = """🚫 <b>Access Denied!</b>  
You are not authorized to use this bot.  

📩 Contact @THe_vK_3 for access!  

💡 <b>Credits:</b> @THe_vK_3
    """

    DOWNLOAD_SUCCESS = """✅ <b>File Downloaded Successfully!</b>  

⏳ Time Taken: <b>{} seconds</b>.  

💡 <b>Credits:</b> @THe_vK_3
    """

    RENAME_PROMPT = """✍️ Send the output file name <b>with extension</b> (or type <code>default</code> to keep it):

📁 Your Current File name:- <code>{}</code>
"""

    FILE_SIZE_ERROR = "❌ <b>ERROR:</b> Unable to extract file size from the URL!\n\n💡 <b>Credits:</b> @Cybrion"
    MAX_FILE_SIZE = "⚠️ <b>File too Large!</b> The maximum file size allowed by Telegram is <b>2GB</b>.\n\n💡 <b>Credits:</b> @THe_vK_3"
    
    LONG_CUS_FILENAME = """⚠️ <b>Filename Too Long!</b>  
The filename you provided exceeds 60 characters.  
Please use a shorter name.  

💡 <b>Credits:</b> @THe_vK_3
    """

    UNSUPPORTED_FORMAT = "❌ <b>ERROR:</b> File format <b>{}</b> is not supported!\n\n💡 <b>Credits:</b> @THe_vK_3"
