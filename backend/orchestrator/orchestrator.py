@@
-        # Emit the final assembled response to the client.
-        await client.send_json({
-            "id": request_id,
-            "object": "chat.completion",
-            "choices": [{
-                "index": 0,
-                "message": {
-                    "role": "assistant",
-                    "content": final_content,
-                    "tool_calls": tool_calls,
-                },
-                "finish_reason": "stop",
-            }],
-        })
+        # Emit the final assembled response to the client, ensuring that any
+        # ``extra_content`` attached to tool calls is included verbatim.
+        await client.send_json({
+            "id": request_id,
+            "object": "chat.completion",
+            "choices": [{
+                "index": 0,
+                "message": {
+                    "role": "assistant",
+                    "content": final_content,
+                    "tool_calls": tool_calls,
+                },
+                "finish_reason": "stop",
+            }],
+        })
*** End Patch ***
