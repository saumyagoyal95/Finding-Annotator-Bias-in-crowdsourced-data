<div align="center">

# Finding annotator bias in crowdsourced data

<img src='https://github.com/saumyagoyal95/Finding-Annotator-Bias-in-crowdsourced-data/blob/9af557fb15ba0da5edb57202abf3d8d8ca8cb424/Crowd-Sourcing-Data.jpg' width=300px> <br>
source : https://www.educba.com/crowdsourcing-data <br>

  
[About](#about) •
[Configuration Requirements](#configuration-requirements) •
[Installation](#installation) •
[Conclusion](#how-to-contribute)  
  
</div>

## 📒 About <a name="about"></a>

Crowdsourcing has experienced a big boom with the increasing interest in obtaining labelled data. It is a powerfulway of obtaining data in a cheaper and faster way. However, annotator biases and spammers can affect the final quality of the models created with this data. 

In the project, 
- I used different end-to-end methods such as Latent Truth Network and Fast Dawid-Skene to try and model annotator-specific biases as bias matrices. These models will help obtain the ground truth estimation with the help of the singlylabelled Organic dataset. 
- I also propose a new method where we combine both models by creating predictions to convert our dataset into a multi-labelled one. 
- I modelled the biases of each annotator to see if their annotations are reliable or not, and to detect possible spammers. 
- I clustered the bias matrices to discover groups of annotators that approach the labelling task in the same way. I was able to find these different groups of annotators and by adding noise to one of the annotators, I was also were able to cluster it as a spammer. 
 
Even though the dataset was quite limited in length, which made the training of bias matrices hard, I was able to show that the approach can indeed model biases for annotator clustering and spammer detection.

## 👨‍💻 Configuration Requirements <a name="configuration-requirements"></a>

What is the required configuration for running this code
1. Jupyter Notebook
2. Libraries used - Pandas, Numpy, MatplotLib, math, pytorch, sklearn.metrics, scipy, torch
3. GloVe encoding 

## 🖥️ Graphs and Findings <a name="installation"></a>

1. Available Latent Network Architecture

<img src='https://github.com/saumyagoyal95/Finding-Annotator-Bias-in-crowdsourced-data/blob/628e9633eff636905c588e650f2c6baf3b8d076b/Images/Base%20Architecture.png' width=350px> <br>

2. Ingenious Hybrid Architecture

<img src='https://github.com/saumyagoyal95/Finding-Annotator-Bias-in-crowdsourced-data/blob/628e9633eff636905c588e650f2c6baf3b8d076b/Images/Hybrid%20Architecture.png' width=350px> <br>

3. Clustering Similar Annotators

<img src='https://github.com/saumyagoyal95/Finding-Annotator-Bias-in-crowdsourced-data/blob/628e9633eff636905c588e650f2c6baf3b8d076b/Images/Clustering.png' width=350px> <br>

4. Spammer Detection

<img src='https://github.com/saumyagoyal95/Finding-Annotator-Bias-in-crowdsourced-data/blob/628e9633eff636905c588e650f2c6baf3b8d076b/Images/Spammer%20Detection.png' width=350px> <br>


## ✍️ Conclusion <a name="how-to-contribute"></a>

- I showed with the help of Latent Truth Network architecture and bias modeling on the
singly-labelled crowdsourced data, how we can create an end-to-end model for finding the bias in the
annotators.
- I figured out how the two approaches of LTNet
and Fast David-Skene (FDS) are different from
each other for the bias modelling. 
   - The LTNet trained
in the end-to-end fashion considering the actual text
for finding the latent truth and learning the attention
vectors during the training and this latent truth then
becomes the common ground for all the annotators
bias matrices. 
   - The Fast David Skene,
the ground truth sentiment for each sentence is found
using the multi-labelled dataset.
We found that the multi-labelled predictions that are
produced from our experiment 1 could be chained to
the FDS input. This particularly works because the
output of our experiment 1 and input of FDS is in line.
This approach can thus help in finding the ground truth
in the singly-labelled dataset, which is considered as a
very difficult task.
- The bias and the confusion matrices produced by
our improved architecture was able to precisely detect
the spammer amongst the annotators. 
- The
clustering of the annotators based on the bias matrix
also seemed to work with our architecture. Also, the
produced bias shows high robustness under very noisy
conditions making the approach potentially usable outside
of lab conditions.
- I worked on just singly-labelled dataset, we tell
that if finding ground truth is not absolutely necessary
we can ignore the chaining part and find the bias in
the annotators with the end-to-end latent truth model
approach, whereas if their is the necessity to find the
ground truth labels chaining of the two architecture
could result in potential outcome.
- I believe it is necessary to conduct more experiments
on more datasets from different sources to solidify
our conclusions regarding our hybrid approach of
finding ground truths, as the singly-labelled crowdsourcing
use case was performed on a very small
dataset. Furthermore, we believe that there might be
many different use cases other than the sentiment analysis
which can be explored.
