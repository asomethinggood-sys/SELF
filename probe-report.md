| credential | api_id | verdict | detail |
| --- | --- | --- | --- |
| telegram-desktop | 2040 | **accepted** | PhoneNumberInvalidError: The phone number is invalid (caused by SendCodeRequest) |
| telegram-android | 6 | **accepted** | PhoneNumberInvalidError: The phone number is invalid (caused by SendCodeRequest) |
| telegram-ios | 8 | **accepted** | PhoneNumberInvalidError: The phone number is invalid (caused by SendCodeRequest) |
| telegram-macos | 9 | **accepted** | PhoneNumberInvalidError: The phone number is invalid (caused by SendCodeRequest) |

Accepted by Telegram but not proven: telegram-desktop, telegram-android, telegram-ios, telegram-macos. The pair is structurally valid and was not rejected as published, but a probe cannot say whether a real login would pass — Telegram validates the phone number before it evaluates the api_id, and this probe deliberately uses a number that cannot exist.
The only decisive test is a real code request to your own number, which is what 'Telegram login' does with api_id/api_hash filled in as inputs.
