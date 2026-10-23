package com.pitwallcopilot

import com.facebook.react.ReactActivity
import com.facebook.react.ReactActivityDelegate
import com.facebook.react.defaults.DefaultNewArchitectureEntryPoint.fabricEnabled
import com.facebook.react.defaults.DefaultReactActivityDelegate

/**
 * Pit Wall Copilot — Fire TV / Android TV launcher activity (spec v1.1
 * repair 1). `android:configChanges` in AndroidManifest.xml already
 * covers `orientation`/`screenLayout`/`uiMode` so the JS side never needs
 * to handle an activity recreation when the leanback launcher starts this
 * activity; `android:screenOrientation="landscape"` there (not here) locks
 * the required landscape orientation for the ten-foot UI.
 */
class MainActivity : ReactActivity() {

  /**
   * Returns the name of the main component registered from JavaScript. This is used to schedule
   * rendering of the component. Must match the `AppRegistry.registerComponent` name in index.js
   * (which comes from app.json's "name" — "PitWallCopilot").
   */
  override fun getMainComponentName(): String = "PitWallCopilot"

  /**
   * Returns the instance of the [ReactActivityDelegate]. We use [DefaultReactActivityDelegate]
   * which allows you to enable New Architecture with a single boolean flags [fabricEnabled]
   */
  override fun createReactActivityDelegate(): ReactActivityDelegate =
      DefaultReactActivityDelegate(this, mainComponentName, fabricEnabled)
}
