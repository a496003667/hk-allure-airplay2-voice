typedef void WiimuContext;

extern WiimuContext *WiimuContextGet(void);
extern void WMUtil_Snd_Ctrl_SetProcessMute(WiimuContext *context, int mute,
                                           int process_id);

#ifndef HK_AIRPLAY_MUTE
#define HK_AIRPLAY_MUTE 0
#endif

__attribute__((constructor)) static void hk_airplay_mute_hook(void) {
  WiimuContext *context = WiimuContextGet();
  if (context != 0) {
    WMUtil_Snd_Ctrl_SetProcessMute(context, HK_AIRPLAY_MUTE, 3);
  }
}
