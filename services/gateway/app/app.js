const express = require('express');
const app = express();
const express = require('express');
const app = express();
app.use(express.json());

app.post('/api/tasks/edit', async (req, res) => {
  try {
    // Existing edit logic
    const { taskId, ...updates } = req.body;
    const updatedTask = await Task.findByIdAndUpdate(taskId, updates, { new: true });
    res.json({ success: true, task: updatedTask });

    console.log('Edit task request received:', req.body);
  } catch (error) {
    res.status(500).json({ error: 'Failed to edit task' });
  }
});

// Start server
const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`Server running on port ${PORT}`);
});
    const { taskId, ...updates } = req.body;
    // Example: Update task in a database
    const updatedTask = await Task.findByIdAndUpdate(taskId, updates, { new: true });
    res.json({ success: true, task: updatedTask });
  } catch (error) {
    res.status(500).json({ error: 'Failed to edit task' });
  }
});
