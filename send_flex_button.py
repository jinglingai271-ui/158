from linebot import LineBotApi
from linebot.models import FlexSendMessage, BubbleContainer, BoxComponent, ButtonComponent, TextComponent, URIAction

line_bot_api = LineBotApi('CUe1Avu/wrK/rZW/k8BQ9GMIGYQBrP9K4i2e1iDJ3W7DJ0PFBcbOqye5uaoIBCA4gKQy1yyyw1P/t2PVOXHD7z7qD8demfUs/1cy3TIuT5THP0qD+nVxSJ/95kwtgrseHl9FRdvnwJlrHx4srmLSCQdB04t89/1O/w1cDnyilFU=')

def send_one_click_button(group_id):
    flex_message = FlexSendMessage(
        alt_text="一鍵整單助理",
        contents=BubbleContainer(
            body=BoxComponent(
                layout='vertical',
                contents=[
                    TextComponent(text="整單助理", weight="bold", size="sm", color="#aaaaaa"),
                    ButtonComponent(
                        style='primary',
                        color='#1DB446',
                        action=URIAction(
                            label='一鍵整單',
                            # 填入你在 LINE Developers 建立的 LIFF URL
                            uri='https://liff.line.me/YOUR_LIFF_ID'
                        )
                    )
                ]
            )
        )
    )
    line_bot_api.push_message(group_id, flex_message)