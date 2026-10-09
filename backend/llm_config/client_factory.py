@@
-        # Provider selection is currently based on a simple substring match
-        # against the model name (e.g. “gemini”).  This is fragile for
-        # continuation metadata handling because it does not bind the request
-        # to a concrete provider profile that knows how to forward extra
-        # content.  The change below makes the selection explicit for Gemini
-        # compatibility.
-        if "gemini" in model_name.lower():
-            return GeminiClient(profile=profile)
+        # Provider selection is now explicit for Gemini compatibility.  The
+        # Gemini client knows how to forward ``extra_content`` (including
+        # thought signatures) without stripping it.  This avoids the previous
+        # heuristic that could select a generic OpenAI client and lose the
+        # metadata.
+        if model_name.lower().startswith("gemini-"):
+            return GeminiClient(profile=profile)
*** End Patch ***
